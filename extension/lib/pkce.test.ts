import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { generateCodeChallenge, generateCodeVerifier } from './pkce';

const UNRESERVED_CHARS = /^[A-Za-z0-9\-._~]+$/;

function expectedChallenge(verifier: string): string {
  return createHash('sha256')
    .update(verifier)
    .digest('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

describe('generateCodeVerifier', () => {
  it('returns a 43-character string using only the RFC 7636 unreserved charset', () => {
    const verifier = generateCodeVerifier();
    expect(verifier).toHaveLength(43);
    expect(verifier).toMatch(UNRESERVED_CHARS);
  });

  it('returns a different value on each call', () => {
    expect(generateCodeVerifier()).not.toBe(generateCodeVerifier());
  });
});

describe('generateCodeChallenge', () => {
  it('matches an independently computed base64url(SHA-256(verifier))', async () => {
    const verifier = generateCodeVerifier();
    const actual = await generateCodeChallenge(verifier);
    expect(actual).toBe(expectedChallenge(verifier));
  });
});
