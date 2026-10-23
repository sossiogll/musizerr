/**
 * The shortest password that can be SET, by registration or by an administrator (the one rule of the server,
 * `core/user_auth.hash_new_password`: it answers 422 `password_too_short`). A password that already exists
 * and is shorter still signs in. The server judges; this only keeps a request from leaving that cannot succeed.
 */
export const MIN_PASSWORD_LENGTH = 8;
