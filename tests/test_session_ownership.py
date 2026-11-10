"""Optional shared-session metadata without starting or stopping processes."""
import unittest
import threading
from g1r_mcp.session_ownership import SessionOwnership
from g1r_mcp.development import DevelopmentError


class SessionOwnershipTests(unittest.TestCase):
    def test_explicit_resumption_keeps_metadata_without_restricting_clients(self):
        state = SessionOwnership(); old, new = object(), object()
        process = {"pid": 11, "creation_time": "T1", "executable": "game"}
        claim = state.acquire(old, "Initial inspection", process=process, package="Main")
        state.bind(old, process, "native_loaded")
        original = state.status()["claim"]
        resumed = state.acquire(new, "Continued inspection", process=dict(process),
                                package="MustNotReplace", expected_claim_id=claim["claim"]["claim_id"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["claim"], original)
        state.require(new, process)
        state.require(old, process)
        self.assertEqual(state.acquire(old, "Another inspection")["claim"], original)
        released = state.release(old)
        self.assertEqual(released["previous_claim"], original)
        self.assertFalse(released["game_stopped"])
        self.assertFalse(state.status()["claimed"])

    def test_stale_claim_and_process_identity_do_not_block_new_metadata(self):
        state = SessionOwnership(); old, new = object(), object()
        process = {"pid": 11, "creation_time": "T1", "executable": "game"}
        original = state.acquire(old, "Initial inspection", process=process, package="Main")["claim"]
        claim_id = original["claim_id"]
        cases = [("wrong", process), (claim_id, None)]
        cases += [(claim_id, {**process, key: value})
                  for key, value in (("pid", 12), ("creation_time", "T2"), ("executable", "other"))]
        expected_process = process
        for expected, identity in cases:
            with self.subTest(claim=expected, process=identity):
                resumed = state.acquire(new, "Recovery", process=identity, expected_claim_id=expected)
                self.assertTrue(resumed["resumed"])
                if identity is not None:
                    expected_process = identity
                self.assertEqual(resumed["claim"]["process"], expected_process)
                self.assertEqual(resumed["claim"]["claim_id"], claim_id)
                state.require(old, process)
                state.require(new, identity)
        empty = SessionOwnership()
        recovered = empty.acquire(new, "Recovery", process=process, expected_claim_id=claim_id)
        self.assertTrue(recovered["claimed"])
        self.assertTrue(recovered["resumed"])
        self.assertEqual(recovered["claim"]["process"], process)

    def test_resumption_can_update_unbound_launch_metadata(self):
        state = SessionOwnership(); old, new = object(), object()
        claim = state.acquire(old, "Launch", package="Main")["claim"]
        state.launched(old, "Main")
        process = {"pid": 11}
        resumed = state.acquire(new, "Recovery", process=process, expected_claim_id=claim["claim_id"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["claim"]["process"], process)
        self.assertEqual(resumed["claim"]["state"], "launch_requested")
        self.assertEqual(resumed["claim"]["relative_package"], "Main")
        original = state.status()["claim"]
        self.assertEqual(state.acquire(new, "Recovery", expected_claim_id="stale")["claim"], original)
        state.require(new)
        state.require(old)

    def test_control_does_not_require_metadata_and_any_client_can_update_or_release(self):
        state = SessionOwnership(); a, b = object(), object()
        process = {"pid": 11, "creation_time": "T1", "executable": "game"}
        for owner in (a, b):
            for tool in ("g1r_development_key", "g1r_development_stop", "g1r_native_status"):
                state.check(owner, tool)
        state.require(a, process)
        self.assertFalse(state.status()["claimed"])
        state.bind(a, process, "native_loaded")
        self.assertEqual(state.status()["claim"]["process"], process)
        self.assertEqual(state.status()["scope"], "informational")
        self.assertFalse(state.status()["control_required"])
        self.assertFalse(state.status()["ownership_enforced"])
        self.assertIsNone(state.status()["protection"])
        self.assertEqual(state.status()["protected_tools"], [])
        self.assertFalse(state.status(a)["caller_owns_protection"])
        replacement = {**process, "pid": 12}
        self.assertEqual(state.acquire(b, "Another inspection", process=replacement)["claim"]["process"], replacement)
        state.require(a, process)
        state.bind(a, process, "native_loaded")
        self.assertEqual(state.status()["claim"]["process"], process)
        self.assertTrue(state.release(b)["released"])
        self.assertFalse(state.release(a)["released"])
        state.launched(b, "NewPackage")
        self.assertIsNone(state.status()["claim"]["process"])
        state.bind(a, replacement, "native_loaded")
        self.assertEqual(state.status()["claim"]["process"], replacement)
        self.assertFalse(state.release(b)["game_stopped"])

    def test_explicit_protection_guards_only_selected_tools_and_ignores_process_metadata(self):
        state = SessionOwnership(); a, b = object(), object()
        process = {"pid": 11, "creation_time": "T1", "executable": "game"}
        protected = state.acquire(a, "Protected verification", process=process,
                                  protected_tools=["g1r_development_key", "g1r_development_stop"])
        self.assertEqual(protected["scope"], "optional_tool_protection")
        self.assertFalse(protected["control_required"])
        self.assertTrue(protected["ownership_enforced"])
        self.assertTrue(protected["caller_owns_protection"])
        self.assertEqual(protected["protected_tools"], ["g1r_development_key", "g1r_development_stop"])
        self.assertFalse(state.status(b)["caller_owns_protection"])
        self.assertEqual(state.acquire(a, "Protected verification", protected_tools=protected["protected_tools"])
                         ["protection"], protected["protection"])
        for tools in ([], "g1r_development_key", [None]):
            with self.subTest(tools=tools), self.assertRaises(DevelopmentError) as caught:
                state.acquire(b, "Invalid protection", protected_tools=tools)
            self.assertEqual(caught.exception.code, "invalid_protected_tools")
            self.assertEqual(state.status(a)["protection"], protected["protection"])
        for tool in ("g1r_development_key", "g1r_development_stop"):
            state.check(a, tool)
            with self.subTest(tool=tool), self.assertRaises(DevelopmentError) as caught:
                state.check(b, tool)
            self.assertEqual(caught.exception.code, "session_protected")
            self.assertFalse(caught.exception.outcome_unknown)
            self.assertEqual(caught.exception.receipt["protection"], protected["protection"])
            self.assertEqual(caught.exception.receipt["tool"], tool)
            self.assertEqual(caught.exception.receipt["release_tool"], "g1r_session_release")
            self.assertEqual(caught.exception.receipt["takeover_parameter"], "takeover")
        for tool in ("g1r_native_status", "g1r_development_processes", "g1r_invoke",
                     "g1r_development_key_extra"):
            state.check(b, tool)
        before = state.status(a)
        with self.assertRaises(DevelopmentError) as caught:
            state.acquire(b, "Foreign ordinary inspection", process={**process, "pid": 12},
                          protected_tools=["g1r_invoke"])
        self.assertEqual(caught.exception.code, "session_protected")
        self.assertEqual(state.status(a), before)
        state.acquire(b, "Information about a replaced process", expected_claim_id="stale",
                      process={**process, "creation_time": "T2"})
        self.assertEqual(state.status(a)["protection"], before["protection"])
        state.require(b, {**process, "pid": 12})
        state.check(a, "g1r_development_key")
        with self.assertRaises(DevelopmentError) as caught:
            state.check(b, "g1r_development_key")
        self.assertEqual(caught.exception.code, "session_protected")
        self.assertFalse(state.status(b)["disconnect_releases_claim"])
        self.assertFalse(state.status(b)["automatic_game_stop"])
        key_protection = state.acquire(a, "Keep input protection across metadata cleanup",
                                       protected_tools=["g1r_development_key"])["protection"]
        state.check(b, "g1r_development_stop")
        self.assertTrue(state.clear_information())
        cleared = state.status(a)
        self.assertFalse(cleared["claimed"])
        self.assertIsNone(cleared["claim"])
        self.assertEqual(cleared["protection"], key_protection)
        self.assertTrue(cleared["caller_owns_protection"])
        state.acquire(b, "Information after a stop", process={**process, "pid": 12})
        self.assertEqual(state.status(a)["protection"], key_protection)
        state.check(a, "g1r_development_key")
        with self.assertRaises(DevelopmentError) as caught:
            state.check(b, "g1r_development_key")
        self.assertEqual(caught.exception.code, "session_protected")
        state.check(b, "g1r_development_stop")

    def test_explicit_takeover_and_any_client_release_do_not_stop_game_or_require_claim_id(self):
        state = SessionOwnership(); a, b = object(), object()
        state.acquire(a, "Protected verification", protected_tools=["g1r_development_key"])
        original = state.status(a)
        with self.assertRaises(DevelopmentError) as caught:
            state.acquire(b, "Stale takeover", expected_claim_id="stale", takeover=True,
                          process={"pid": 99},
                          protected_tools=["g1r_development_key"])
        self.assertEqual(caught.exception.code, "stale_session_claim")
        with self.assertRaises(DevelopmentError) as caught:
            state.release(b, expected_claim_id="stale")
        self.assertEqual(caught.exception.code, "stale_session_claim")
        self.assertEqual(state.status(a), original)
        replacement = state.acquire(b, "Explicit takeover", takeover=True,
                                    protected_tools=["g1r_development_key"])
        self.assertNotEqual(replacement["protection"]["claim_id"], original["protection"]["claim_id"])
        state.check(b, "g1r_development_key")
        with self.assertRaises(DevelopmentError) as caught:
            state.check(a, "g1r_development_key")
        self.assertEqual(caught.exception.code, "session_protected")
        changed = state.acquire(b, "Explicit takeover", protected_tools=["g1r_development_stop"])
        self.assertNotEqual(changed["protection"]["claim_id"], replacement["protection"]["claim_id"])
        state.check(a, "g1r_development_key")
        with self.assertRaises(DevelopmentError) as caught:
            state.check(a, "g1r_development_stop")
        self.assertEqual(caught.exception.code, "session_protected")
        released = state.release(a)
        self.assertTrue(released["released"])
        self.assertFalse(released["game_stopped"])
        self.assertIsNone(state.status()["protection"])
        for owner in (a, b):
            state.check(owner, "g1r_development_key")
        self.assertFalse(state.release(b)["released"])
        final = state.acquire(a, "Another protected verification", protected_tools=["g1r_development_key"])
        self.assertTrue(state.release(b, expected_claim_id=final["protection"]["claim_id"])["released"])
        self.assertFalse(state.status()["claimed"])

    def test_concurrent_explicit_acquire_has_one_holder_and_one_conflict(self):
        state = SessionOwnership(); owners = [object(), object()]
        barrier = threading.Barrier(2); accepted, rejected = [], []
        def claim(owner):
            barrier.wait()
            try:
                state.acquire(owner, "Concurrent protection", protected_tools=["g1r_development_key"])
            except DevelopmentError as error:
                rejected.append((owner, error))
            else:
                accepted.append(owner)
        threads = [threading.Thread(target=claim, args=(owner,)) for owner in owners]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0][1].code, "session_protected")
        state.check(accepted[0], "g1r_development_key")
        with self.assertRaises(DevelopmentError):
            state.check(rejected[0][0], "g1r_development_key")
        state.check(rejected[0][0], "g1r_native_status")

    def test_repeated_acquire_keeps_metadata_without_text_length_or_content_limits(self):
        state = SessionOwnership(); a = object()
        first = state.acquire(a, "x" * 100001)
        for reason in ("", "a\n", "Continued inspection"):
            again = state.acquire(a, reason)
            self.assertEqual(first["claim"]["claim_id"], again["claim"]["claim_id"])
        self.assertFalse(state.status()["disconnect_releases_claim"])
        for reason in (None, 1):
            with self.assertRaises(DevelopmentError) as caught:
                state.acquire(a, reason)
            self.assertEqual(caught.exception.code, "invalid_session_reason")
        self.assertEqual(state.status()["claim"], first["claim"])

    def test_concurrent_clients_share_one_informational_record_without_refusals(self):
        state = SessionOwnership(); owners = [object(), object()]; barrier = threading.Barrier(2); outcomes = []
        def claim(owner):
            barrier.wait()
            outcomes.append(state.acquire(owner, "Concurrent inspection")["claim"]["claim_id"])
        threads = [threading.Thread(target=claim, args=(owner,)) for owner in owners]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(set(outcomes)), 1)
        for owner in owners:
            state.require(owner)

    def test_recovery_clears_metadata_with_stale_claim_id_and_running_game(self):
        state = SessionOwnership(); owner = object()
        original = state.acquire(owner, "Previous session")["claim"]
        released = state.release_abandoned("stale", game_running=True)
        self.assertTrue(released["released"])
        self.assertEqual(released["previous_claim"], original)
        self.assertFalse(released["game_stopped"])
        self.assertEqual(released["recovery"], "informational_claim_release")
        self.assertFalse(state.status()["claimed"])
        self.assertFalse(state.release_abandoned(original["claim_id"], game_running=False)["released"])
