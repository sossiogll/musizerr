/**
 * Where Musizeer comes from: the About page (routes/about.tsx) shows it to every viewer.
 *
 * NOTICE.md (additional terms of the license, section 7) asks every copy and every work
 * based on Musizeer to keep the attribution notice, word for word, on a page reachable
 * from the main navigation, and a modified version that users reach over a network to
 * offer them its source (AGPL section 13). A fork changes SOURCE_URL to its own source;
 * it never removes or rewords ATTRIBUTION.
 */
export const UPSTREAM_URL = "https://github.com/sossiogll/musizeer";

/** The attribution notice of NOTICE.md, verbatim: never translated. */
export const ATTRIBUTION = `Based on Musizeer — ${UPSTREAM_URL}`;

/** The source code of THIS instance's version (AGPL section 13). */
export const SOURCE_URL = UPSTREAM_URL;

export const COPYRIGHT = "Copyright (C) 2026 Sossio Giallaurito and the Musizeer contributors.";

export const LICENSE_URL = `${UPSTREAM_URL}/blob/master/LICENSE`;
export const NOTICE_URL = `${UPSTREAM_URL}/blob/master/NOTICE.md`;
