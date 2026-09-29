// Project-specific build-time env vars, merged into WXT's generated ImportMetaEnv
// (.wxt/types/globals.d.ts). Only WXT_/VITE_-prefixed vars reach the bundle.
interface ImportMetaEnv {
  readonly WXT_BACKEND_BASE_URL?: string;
}
