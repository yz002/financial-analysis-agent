import { defineConfig } from 'vitest/config';
import { WxtVitest } from 'wxt/testing/vitest-plugin';

export default defineConfig({
  // mode: 'test' -- without it, WxtVitest resolves WXT's config in its "serve" default
  // mode (development) and loads .env.development into process.env, which Vitest's
  // import.meta.env then reflects: tests would see the real local backend URL instead of
  // .env.test's deliberately unresolvable one.
  plugins: [WxtVitest({ mode: 'test' })],
});
