import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { BACKEND_BASE_URL } from './authConfig';
import { hostPermissionFor, resolveBackendBaseUrl } from './backendUrl';

const RENDER_ORIGIN = 'https://financial-analysis-agent-5450.onrender.com';
const LOCAL_ORIGIN = 'http://127.0.0.1:8000';

describe('resolveBackendBaseUrl', () => {
  it('accepts an https origin', () => {
    expect(resolveBackendBaseUrl(RENDER_ORIGIN)).toBe(RENDER_ORIGIN);
  });

  it('accepts plain http only for a local backend', () => {
    expect(resolveBackendBaseUrl(LOCAL_ORIGIN)).toBe(LOCAL_ORIGIN);
    expect(resolveBackendBaseUrl('http://localhost:8000')).toBe('http://localhost:8000');
    expect(() => resolveBackendBaseUrl('http://example.com')).toThrow(/https/);
  });

  it.each([undefined, ''])('refuses a missing value (%j)', (raw) => {
    expect(() => resolveBackendBaseUrl(raw)).toThrow(/not set/);
  });

  it('refuses a value that is not a URL', () => {
    expect(() => resolveBackendBaseUrl('not a url')).toThrow(/not a valid URL/);
  });

  it.each([
    `${RENDER_ORIGIN}/`,
    `${RENDER_ORIGIN}/v1`,
    `${RENDER_ORIGIN}?x=1`,
    `${RENDER_ORIGIN}#x`,
  ])('refuses anything beyond an origin (%s)', (raw) => {
    expect(() => resolveBackendBaseUrl(raw)).toThrow(/origin only/);
  });
});

describe('hostPermissionFor', () => {
  it('matches every path on the origin', () => {
    expect(hostPermissionFor(RENDER_ORIGIN)).toBe(`${RENDER_ORIGIN}/*`);
  });
});

// Regression guard on the committed per-mode env files themselves: WXT picks
// .env.[mode] at build time, so an accidental edit here would silently retarget a build.
function readEnvFile(fileName: string): string {
  return readFileSync(fileURLToPath(new URL(`../${fileName}`, import.meta.url)), 'utf-8');
}

function envFileBackendUrl(fileName: string): string | undefined {
  return readEnvFile(fileName).match(/^WXT_BACKEND_BASE_URL=(.*)$/m)?.[1]?.trim();
}

describe('per-mode env files', () => {
  it('production builds target the Render backend, never localhost', () => {
    expect(resolveBackendBaseUrl(envFileBackendUrl('.env.production'))).toBe(RENDER_ORIGIN);
    expect(readEnvFile('.env.production')).not.toMatch(/127\.0\.0\.1|localhost/);
  });

  it('development builds target the local backend', () => {
    expect(resolveBackendBaseUrl(envFileBackendUrl('.env.development'))).toBe(LOCAL_ORIGIN);
  });

  it('tests run against an unresolvable backend, never a real one', () => {
    expect(BACKEND_BASE_URL).toBe('https://backend.invalid');
  });
});
