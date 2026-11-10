# Square OAuth (read-only, PKCE)

Driver Coach reads Square payments and team members for the live ticker and the no-idle last-sale check. Those calls can use a seller-granted OAuth token instead of a personal access token. The server never sees the Square application secret.

OAuth is off unless `SQUARE_OAUTH_APP_ID` is set. Leave it unset and the process keeps today's order: `SQUARE_ACCESS_TOKEN`, then `SQUARE_TOKEN=` in `~/.hermes/secrets/softeedashboard.env`.

## Production application

Set the production application id in the environment. Do not put it in code.

```
SQUARE_OAUTH_APP_ID=sq0idp-IcgFfAXRpjHszcKxCuGSFQ
```

In the Square Developer Console, production OAuth, register this redirect URL exactly:

```
https://driver-coach-backend-test.onrender.com/driver-coach/v1/square/oauth/callback
```

That same value is sent as `redirect_uri` on both the authorize redirect and the token request. Override it with `SQUARE_OAUTH_REDIRECT_URI` if the registered URL changes. The connect link uses that URL's origin.

`DC_OFFICE_KEY` gates the link and status routes. Compare is constant time. The key is never a query parameter.

## Scopes

The authorize request asks for two read scopes and nothing else:

| Scope | Why |
| --- | --- |
| `PAYMENTS_READ` | `GET /v2/payments` for the ticker and last sale (amount, status, time, team member, note, device). |
| `EMPLOYEES_READ` | `POST /v2/team-members/search` to map a team member id to the driver name. |

`ORDERS_READ` is not requested. Nothing calls the Orders API. `MERCHANT_PROFILE_READ` is not requested. Location ids are fixed in code, and the merchant id comes back on the token response. No `*_WRITE` scope is requested.

## Connect

Apply `backend/migrations/010_square_oauth.sql` in the service schema (`DRIVER_COACH_SCHEMA`, `driver_coach_test` on the pilot). The migration refuses to run in `public`.

1. `POST /driver-coach/v1/square/oauth/link` with header `X-DC-Office-Key`.
2. The response is `{ "url", "expiresAt" }`. `url` looks like `https://driver-coach-backend-test.onrender.com/driver-coach/v1/square/oauth/start?t=<nonce>` and expires in 30 minutes.
3. Open that URL. It does not contain the office key. `k` and `X-DC-Office-Key` do not authorize `/start`.
4. `GET /start` does not use the nonce. While the nonce is valid and unused, the response is a confirm page titled "Connect Square to Driver Coach". The page says the connection requests read-only access to payments and team members, and its only button is "Continue to Square". That button posts form field `t` to `/start`. The response sends `Cache-Control: no-store` and `Referrer-Policy: no-referrer`, and the page has a `noindex` meta tag. A link preview, a second GET, or a browser handoff shows the same page and still does not use the nonce.
5. `POST /start` uses the nonce once, in the same database transaction that stores the PKCE verifier, and answers `303` to `https://connect.squareup.com/oauth2/authorize` with `session=false`, an `S256` challenge, and the two read scopes. Square then sends the browser on to `https://squareup.com/oauth2/authorize`.
6. If that same `t` is opened again, by GET or POST, within 10 minutes — and sooner if the PKCE state has expired — while that state is still unused, the server does not show an error and does not create a second state. It shows the same confirm page, and Continue goes to the same Square authorize URL. The verifier stays in `square_oauth_state` and is only read to rebuild the challenge.
7. The error page is reserved for an unknown nonce, an expired unused nonce, a replay outside that 10-minute window, an expired PKCE state, or a state whose callback already ran. After the callback, when status is connected, the page says "Square is already connected".
8. Square redirects to the callback. The browser shows "Driver Coach now has read-only Square access".

The nonce is random. The database stores only its SHA-256 hash. That hash is also the PKCE `state` primary key, so a later visit can find the verifier that is already stored. Nothing copies the verifier into another table or column, and no migration past 010 is required. The state itself lasts 15 minutes. The callback still accepts it only once and clears the verifier when it runs.

The token exchange sends `grant_type=authorization_code`, `client_id`, `code`, `code_verifier`, and `redirect_uri`. It does not send the application secret.

`GET /driver-coach/v1/square/oauth/status` (same office-key header) returns:

```json
{
  "connected": true,
  "merchantId": "MERCHANT",
  "scopes": ["PAYMENTS_READ", "EMPLOYEES_READ"],
  "expiresAt": "2026-12-10T19:00:00Z",
  "lastRefreshAt": null,
  "lastError": null
}
```

Tokens are not in this body, the HTML pages, or the logs.

If `DC_OFFICE_KEY` is unset, `/link` and `/status` return 503. A wrong key is 401. If `SQUARE_OAUTH_APP_ID` is unset, `/link`, `/start` (GET and POST), `/callback`, and `/status` return 404. If migration 010 is not applied, the routes return 503 and Square calls fall back to the env token and then the file.

## Token use and refresh

Square calls (`live_ticker`, last sale on the route poll) resolve a token in this order:

1. A usable OAuth access token from `square_oauth_token`.
2. `SQUARE_ACCESS_TOKEN`.
3. `SQUARE_TOKEN=` in the Hermes env file.

The OAuth access token is cached in memory for 30 seconds. It is refreshed when it expires within one day, and again if Square returns 401. PKCE refresh tokens are single-use, so the new refresh token is written in the same transaction that clears the claim. A row lock is held only to claim the refresh and to store the result. The Square HTTP call happens after that lock is released. Overlapping refreshes share one flight: the loser waits and uses the rotated token instead of spending the same refresh token twice.

`lastRefreshAt` stays null until the first refresh. `lastError` is a short error code, not a token or a Square body.
