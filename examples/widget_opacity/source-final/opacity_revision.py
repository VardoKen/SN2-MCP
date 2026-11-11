"""UI modification prototype; invoke only against the inspected live widget."""
OPACITY = 0.75
async def apply(client, handle, signature_token):
    return await client.call_tool("sn2_invoke", {"handle": handle,
        "function": "SetRenderOpacity", "arguments": {"InOpacity": OPACITY},
        "expected_signature_token": signature_token})
