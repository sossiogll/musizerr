/**
 * The version of this web app (ADR-0007 §2), fixed by the build from the release tag
 * (`MUSIZEER_VERSION`: "1.4.0", or "1.3.2+7.abc1234" for a build of master); the development server is "dev".
 */
export const WEB_VERSION: string = import.meta.env.VITE_MUSIZEER_VERSION || "dev";
