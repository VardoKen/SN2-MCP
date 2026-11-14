// Copyright (c) 2011 Google, Inc.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.
//
// CityHash, by Geoff Pike and Jyrki Alakuijala
//
// This file provides CityHash64() and related functions.
//
// It's probably possible to create even faster hash functions by
// writing a program that systematically explores some of the space of
// possible hash functions, by using SIMD instructions, or by
// Extracted 64-bit path from the acquired custom UE source; engine types and
// memory helpers are replaced by fixed-width types and standard C++ helpers.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <utility>
#include "sn2_mcp/runtime_cityhash.hpp"
namespace sn2_mcp::runtime_package {
using uint64=std::uint64_t;using uint32=std::uint32_t;using uint8=std::uint8_t;
struct Uint128_64 {uint64 lo,hi;};
static uint64 Fetch64(const char* value){uint64 result;std::memcpy(&result,value,8);return result;}
static uint32 Fetch32(const char* value){uint32 result;std::memcpy(&result,value,4);return result;}
static uint64 bswap_64(uint64 value){return ((value&0xffull)<<56)|((value&0xff00ull)<<40)|((value&0xff0000ull)<<24)|((value&0xff000000ull)<<8)|((value>>8)&0xff000000ull)|((value>>24)&0xff0000ull)|((value>>40)&0xff00ull)|(value>>56);}
static uint64 CityHash128to64(Uint128_64 value){const uint64 mul=0x9ddfea08eb382d69ull;uint64 a=(value.lo^value.hi)*mul;a^=a>>47;uint64 b=(value.hi^a)*mul;b^=b>>47;b*=mul;return b;}
namespace CityHash_Internal {constexpr uint64 k0=0xc3a5c85c97cb3127ull,k1=0xb492b66fbe98f273ull,k2=0x9ae16a3b2f90404full;}
static uint64 Rotate(uint64 val, int shift) {
	// Avoid shifting by 64: doing so yields an undefined result.
	return shift == 0 ? val : ((val >> shift) | (val << (64 - shift)));
}

static uint64 ShiftMix(uint64 val) {
	return val ^ (val >> 47);
}

static uint64 HashLen16(uint64 u, uint64 v) {
	return CityHash128to64({ u, v });
}

static uint64 HashLen16(uint64 u, uint64 v, uint64 mul) {
	// Murmur-inspired hashing.
	uint64 a = (u ^ v) * mul;
	a ^= (a >> 47);
	uint64 b = (v ^ a) * mul;
	b ^= (b >> 47);
	b *= mul;
	return b;
}

static uint64 HashLen0to16(const char *s, uint32 len) {
	using namespace CityHash_Internal;

	if (len >= 8) {
		uint64 mul = k2 + len * 2;
		uint64 a = Fetch64(s) + k2;
		uint64 b = Fetch64(s + len - 8);
		uint64 c = Rotate(b, 37) * mul + a;
		uint64 d = (Rotate(a, 25) + b) * mul;
		return HashLen16(c, d, mul);
	}
	if (len >= 4) {
		uint64 mul = k2 + len * 2;
		uint64 a = Fetch32(s);
		return HashLen16(len + (a << 3), Fetch32(s + len - 4), mul);
	}
	if (len > 0) {
		uint8 a = s[0];
		uint8 b = s[len >> 1];
		uint8 c = s[len - 1];
		uint32 y = static_cast<uint32>(a) + (static_cast<uint32>(b) << 8);
		uint32 z = len + (static_cast<uint32>(c) << 2);
		return ShiftMix(y * k2 ^ z * k0) * k2;
	}
	return k2;
}

// This probably works well for 16-byte strings as well, but it may be overkill
// in that case.
static uint64 HashLen17to32(const char *s, uint32 len) {
	using namespace CityHash_Internal;

	uint64 mul = k2 + len * 2;
	uint64 a = Fetch64(s) * k1;
	uint64 b = Fetch64(s + 8);
	uint64 c = Fetch64(s + len - 8) * mul;
	uint64 d = Fetch64(s + len - 16) * k2;
	return HashLen16(Rotate(a + b, 43) + Rotate(c, 30) + d,
		a + Rotate(b + k2, 18) + c, mul);
}

// Return a 16-byte hash for 48 bytes.  Quick and dirty.
// Callers do best to use "random-looking" values for a and b.
static Uint128_64 WeakHashLen32WithSeeds(
	uint64 w, uint64 x, uint64 y, uint64 z, uint64 a, uint64 b) {
	a += w;
	b = Rotate(b + a + z, 21);
	uint64 c = a;
	a += x;
	a += y;
	b += Rotate(a, 44);
	return { (a + z), (b + c) };
}

// Return a 16-byte hash for s[0] ... s[31], a, and b.  Quick and dirty.
static Uint128_64 WeakHashLen32WithSeeds(
	const char* s, uint64 a, uint64 b) {
	return WeakHashLen32WithSeeds(Fetch64(s),
		Fetch64(s + 8),
		Fetch64(s + 16),
		Fetch64(s + 24),
		a,
		b);
}

// Return an 8-byte hash for 33 to 64 bytes.
static uint64 HashLen33to64(const char *s, uint32 len) {
	using namespace CityHash_Internal;

	uint64 mul = k2 + len * 2;
	uint64 a = Fetch64(s) * k2;
	uint64 b = Fetch64(s + 8);
	uint64 c = Fetch64(s + len - 24);
	uint64 d = Fetch64(s + len - 32);
	uint64 e = Fetch64(s + 16) * k2;
	uint64 f = Fetch64(s + 24) * 9;
	uint64 g = Fetch64(s + len - 8);
	uint64 h = Fetch64(s + len - 16) * mul;
	uint64 u = Rotate(a + g, 43) + (Rotate(b, 30) + c) * 9;
	uint64 v = ((a + g) ^ d) + f + 1;
	uint64 w = bswap_64((u + v) * mul) + h;
	uint64 x = Rotate(e + f, 42) + c;
	uint64 y = (bswap_64((v + w) * mul) + g) * mul;
	uint64 z = e + f + c;
	a = bswap_64((x + z) * mul + y) + b;
	b = ShiftMix((z + a) * mul + d + h) * mul;
	return b + x;
}

std::uint64_t city_hash64(const char *s, std::uint32_t len) {
	using namespace CityHash_Internal;

	if (len <= 32) {
		if (len <= 16) {
			return HashLen0to16(s, len);
		}
		else {
			return HashLen17to32(s, len);
		}
	}
	else if (len <= 64) {
		return HashLen33to64(s, len);
	}

	// For strings over 64 bytes we hash the end first, and then as we
	// loop we keep 56 bytes of state: v, w, x, y, and z.
	uint64 x = Fetch64(s + len - 40);
	uint64 y = Fetch64(s + len - 16) + Fetch64(s + len - 56);
	uint64 z = HashLen16(Fetch64(s + len - 48) + len, Fetch64(s + len - 24));
	Uint128_64 v = WeakHashLen32WithSeeds(s + len - 64, len, z);
	Uint128_64 w = WeakHashLen32WithSeeds(s + len - 32, y + k1, x);
	x = x * k1 + Fetch64(s);

	// Decrease len to the nearest multiple of 64, and operate on 64-byte chunks.
	len = (len - 1) & ~static_cast<uint32>(63);
	do {
		x = Rotate(x + y + v.lo + Fetch64(s + 8), 37) * k1;
		y = Rotate(y + v.hi + Fetch64(s + 48), 42) * k1;
		x ^= w.hi;
		y += v.lo + Fetch64(s + 40);
		z = Rotate(z + w.lo, 33) * k1;
		v = WeakHashLen32WithSeeds(s, v.hi * k1, x + w.lo);
		w = WeakHashLen32WithSeeds(s + 32, z + w.hi, y + Fetch64(s + 16));
		std::swap(z, x);
		s += 64;
		len -= 64;
	} while (len != 0);
	return HashLen16(HashLen16(v.lo, w.lo) + ShiftMix(y) * k1 + z,
		HashLen16(v.hi, w.hi) + x);
}

} // namespace sn2_mcp::runtime_package
