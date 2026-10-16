/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** The version of the web app (ADR-0007 §2): the release tag or the build of master; unset in development. */
  readonly VITE_MUSIZERR_VERSION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
