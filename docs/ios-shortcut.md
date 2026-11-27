# Share to PriceWatch from iPhone

iOS Safari doesn't support web-app share targets, so use a Shortcut. It appears in the
share sheet of Safari and most shopping apps.

## 1. Make PriceWatch reachable from the phone

Follow [remote-access.md](remote-access.md) first. It sets up a private Tailscale network
with HTTPS and an access token, which is the safe way. You'll end up with an address like
`https://my-laptop.example-tailnet.ts.net` and a token.

PriceWatch also has to be **running** when you share, or the Shortcut will fail to connect.
To keep it ready, run this once on the Mac:

```
python3 -m pricewatch autostart on
```

## 2. Build the Shortcut (Shortcuts app → +)

1. Tap the **ⓘ** details button → turn on **Show in Share Sheet** → accept **URLs** and **Text**.
2. Add **Ask for Input** → Prompt "Check how often?" → Default answer `1d`
   (any of `15m`, `6h`, `1d`, `1w`, `1mo`, `1y`).
3. Add **Get Contents of URL**:
   - URL: `https://my-laptop.example-tailnet.ts.net/api/share`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <your token>`
   - Request Body: **JSON**
     - `text` = *Shortcut Input*
     - `frequency` = *Provided Input*
4. Add **Get Dictionary Value** → key `message` → **Show Notification**.

Name it "Watch price". Then share any product link → **Watch price**.

The token is kept in the Shortcut on your phone. It's never in the repo, and it's never
shown to an AI agent. If the phone is lost, rotate the token: generate a new one, put it
in the env file, and restart.

## Fair Price from iPhone (optional)

To compare the price *you* see in an app with the anonymous price, add another
**Ask for Input** ("Price you see?", number) and send it as `seen` in the JSON body.
