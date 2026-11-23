# Share to PriceWatch from iPhone

iOS Safari doesn't support web-app share targets, so use a Shortcut. It appears in the
share sheet of Safari and most shopping apps.

## 1. Make PriceWatch reachable from the phone

The simplest safe option is [Tailscale](https://tailscale.com) on both the computer and
the phone. In your env file (`~/.config/pricewatch/pricewatch.env`):

```
PW_HOST=0.0.0.0
PW_ALLOWED_HOSTS=my-laptop.tailnet-name.ts.net,100.x.y.z
PW_ACCESS_TOKEN=<output of: python -m pricewatch gen-token>
```

Restart PriceWatch.

## 2. Build the Shortcut (Shortcuts app → +)

1. Tap the **ⓘ** details button → turn on **Show in Share Sheet** → accept **URLs** and **Text**.
2. Add **Ask for Input** → Prompt "Check how often?" → Default answer `1d`
   (any of `15m`, `6h`, `1d`, `1w`, `1mo`, `1y`).
3. Add **Get Contents of URL**:
   - URL: `http://my-laptop.tailnet-name.ts.net:8765/api/share`
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
