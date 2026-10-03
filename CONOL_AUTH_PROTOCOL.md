# Conol.ai Auth Protocol (verified 2026-10-02)

## Architecture
- Site is behind **Vercel Security Checkpoint** (Turnstile). Browser JS execution required to pass.
- Once past Vercel: auth = **better-auth** (open-source auth library).
- Captcha = **Google reCAPTCHA v3**, sitekey `6Lc3wmAtAAAAAB9YBPXQtT9uGGsH3ul6LQBc5AUu`.
- Captcha token sent in HTTP header `x-captcha-response`, NOT in request body.

## Endpoints

### 1. Registration: `POST /api/invites/register`
```
POST https://conol.ai/api/invites/register
Content-Type: application/json
x-captcha-response: <reCAPTCHA v3 token (action: "sign_up")>
Accept: application/json

{
  "token": null,
  "email": "baradok609+conol<timestamp>@gmail.com",
  "password": "<from config.json: conol.password>",
  "name": "Conol<6digits>",
  "referrer_share_id": null
}
```
- Response: 200/201 on success
- Requires captcha action: `sign_up`

### 2. Send Verification Email: `POST /api/auth/send-verification-email`
```
POST https://conol.ai/api/auth/send-verification-email
Content-Type: application/json
x-captcha-response: <reCAPTCHA v3 token (action: "send_verification_email")>
Accept: application/json

{
  "email": "baradok609+conol<timestamp>@gmail.com",
  "callbackURL": "/home"
}
```
- Response: 200/201 on success
- Email sent to alias. Captcha action: `send_verification_email`

### 3. Verify Email (from email link)
```
GET https://conol.ai/api/auth/verify-email?token=<verify_token>&callbackURL=%2Fhome
```
- Extract verify URL from Gmail IMAP (sent from "Conol", To: alias)
- Email regex: `https://conol\.ai/api/auth/verify-email\?token=([^&\s"'<>]+)(?:&amp;|&)callbackURL=([^\s"'<>]+)`
- Navigate browser to this URL. Response 200 sets emailVerified=true.

### 4. Sign-In: `POST /api/auth/sign-in/email` ✅ **WORKING VIA HTTP API**
```
POST https://conol.ai/api/auth/sign-in/email
Content-Type: application/json
x-captcha-response: <reCAPTCHA v3 token (action: "sign_in")>
Accept: application/json
Origin: https://conol.ai
Referer: https://conol.ai/login

{
  "email": "baradok609+conol<alias>@gmail.com",
  "password": "<from config.json: conol.password>",
  "callbackURL": "/home",
  "rememberMe": true
}
```
- Response 200:
```json
{
  "redirect": true,
  "token": "hYnObOwibDiiwgIclXY8Tg4Z4IM7vzyM",
  "url": "/home",
  "user": {
    "name": "Conol471446",
    "email": "baradok609+conol5413471446@gmail.com",
    "emailVerified": true,
    "id": "mzGxUrMrYgU9fU0GmHRBtDHgONk2Wcbr",
    "role": "user",
    "banned": false
  }
}
```
- **Set-Cookie**: `__Secure-better-auth.session_token=<token>; Max-Age=604800; Domain=conol.ai; Path=/; HttpOnly; Secure; SameSite=Lax`
- Also sets `__Secure-better-auth.session_token_multi-<hash>=<same_token>` (for multi-session)
- Cookie value format: `<token>.<base64_encoded_signature>`
- **Captcha action**: `sign_in`
- **Token lifetime**: 604800 seconds (7 days) from issue
- **NOTE**: reCAPTCHA v3 tokens have ~2 minute validity. Must be used immediately after solving.

### 5. Get Session: `GET /api/auth/get-session`
```
GET https://conol.ai/api/auth/get-session
Cookie: __Secure-better-auth.session_token=<token>
Accept: application/json
```
- Response 200:
```json
{
  "user": {
    "email": "baradok609+conol5413471446@gmail.com",
    "name": "Conol471446",
    "emailVerified": true,
    "id": "mzGxUrMrYgU9fU0GmHRBtDHgONk2Wcbr",
    "role": "user",
    "banned": false,
    "createdAt": "2026-07-30T12:11:23.320Z",
    "updatedAt": "2026-07-30T12:11:38.803Z"
  }
}
```
- Response 401 if token expired/invalid: `{"error":"Not authenticated"}`
- **This is THE validator**: a 200 response with matching email = account is live.

### 6. Billing Balance: `GET /api/billing/balance`
```
GET https://conol.ai/api/billing/balance
Cookie: __Secure-better-auth.session_token=<token>
Accept: application/json
```
- Response 200:
```json
{
  "userId": "mzGxUrMrYgU9fU0GmHRBtDHgONk2Wcbr",
  "dailyCredits": 100,
  "dailyRefilledAt": "2026-10-02T21:01:07.379Z",
  "subscriptionCredits": 0,
  "subscriptionAmount": 0,
  "extraCredits": 429.86,
  "total": 529.86,
  "updatedAt": "2026-10-02T..."
}
```

### 7. Other Endpoints
- `GET /api/agent-servers` — list agent servers
- `GET /api/workspaces` — list workspaces
- `GET /api/sessions/active` — active sessions

## Captcha Solving (verified working method)

**Provider**: AntiCaptcha
**Task type**: `RecaptchaV3TaskProxyless`
**Parameters**:
```json
{
  "type": "RecaptchaV3TaskProxyless",
  "websiteURL": "https://conol.ai",
  "websiteKey": "6Lc3wmAtAAAAAB9YBPXQtT9uGGsH3ul6LQBc5AUu",
  "pageAction": "sign_in",
  "minScore": 0.3
}
```
- Cost per solve: ≈ $0.01-0.02 (AntiCaptcha reCAPTCHA v3)
- Token length: 2000-2500 chars
- Token valid for: ~2 minutes
- Live keys: 2 AntiCaptcha keys ($43.14 + $2.47 total)

## Session Cookie Format
- Name: `__Secure-better-auth.session_token`
- Value: `<jwt_token>.<base64_signature>`
- URL-encoded throughout: dots and %2B, %2F, %3D
- Store in cookies JSON as `{"name":"__Secure-better-auth.session_token","value":"<raw>","domain":"conol.ai",...}`

## Known Issues
- **2captcha keys**: ALL 20 keys dead (`ERROR_KEY_DOES_NOT_EXIST`), despite showing $1034 claimed balance
- **CapSolver**: balance $0.0002 (near-zero)
- **AntiCaptcha**: 2 live keys, total $45.61
- Can NOT hit conol.ai from HTTP without captcha token (returns 400 `CAPTCHA_MISSING`)
- Can NOT solve captcha as Turnstile via captcha services — it IS reCAPTCHA v3, not Turnstile
- Old cookies from July 2026 all expired (token expires ~7 days after issue)