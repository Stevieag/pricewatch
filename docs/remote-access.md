# Using PriceWatch from your phone or another computer

## First: do you need to?

**Probably not.** By default PriceWatch only listens on your own computer (`127.0.0.1`),
so nothing else, on your Wi-Fi or on the internet, can reach it. That's the safest setup,
and everything works: the watchlist, alerts, the browser bookmarklet and the scheduled checks.

Remote access only adds convenience, such as checking prices or sharing a product from
your phone while you're out. Every way of doing it adds some risk, so only set it up if
you'll use it.

## Recommended: a private network with Tailscale

[Tailscale](https://tailscale.com) connects your own devices over an encrypted private
network (built on WireGuard). PriceWatch stays off the public internet: only devices signed
in to **your** Tailscale account can reach it, and the connection uses HTTPS.

### Set it up

1. **Install Tailscale** on the computer running PriceWatch and on your phone. Sign in to
   the same account on both.
2. **Give PriceWatch an access token and allow your Tailscale address.** On the computer:
   ```bash
   python3 -m pricewatch set-token
   ```
   This saves a new token in your config file, checks that it really was saved, and only
   then shows it. Store it in your password manager. Then add your computer's Tailscale
   name, shown in the Tailscale app (e.g. `my-laptop.example-tailnet.ts.net`), to
   `PW_ALLOWED_HOSTS` in `~/.config/pricewatch/pricewatch.env`.

   PriceWatch refuses to start if `PW_ALLOWED_HOSTS` lists another device's address but no
   token is set, so a skipped or failed token step can't leave it open.
3. **Share PriceWatch on your tailnet only, with HTTPS:**
   ```bash
   tailscale serve --bg 8765
   python3 -m pricewatch stop && python3 -m pricewatch start
   ```
   PriceWatch itself stays bound to `127.0.0.1`; Tailscale passes your devices' requests to it.
4. **On your phone,** open `https://<your computer's Tailscale name>` and sign in with the
   token. *Add to Home Screen* gives you an app-like icon.

The computer has to be awake with PriceWatch running; `python3 -m pricewatch autostart on`
(macOS) takes care of the running part.

### Keep it safe

- **Keep Tailscale up to date** on every device and leave automatic updates on. Check
  [Tailscale's security bulletins](https://tailscale.com/security-bulletins) now and then,
  especially for anything mentioning *Serve*, the feature used here.
- **Don't turn on Tailscale Funnel** for PriceWatch. Funnel publishes a service to the
  public internet; `serve` keeps it private to your devices.
- **Don't turn on Tailscale SSH** unless you need it. PriceWatch doesn't, and it's an extra
  way into your computer.
- **Protect your Tailscale account** with two-factor authentication. Anyone who can sign
  in to it can add devices to your network. For extra protection, turn on
  [Tailnet Lock](https://tailscale.com/kb/1226/tailnet-lock), so new devices need your
  approval.
- **Keep PriceWatch's access token set,** even on your private network. If a device is
  lost, run `python3 -m pricewatch set-token` again and restart, which replaces the token
  everywhere. Also remove old devices from your Tailscale admin page.

### Turn it off again

```bash
tailscale serve reset
```

PriceWatch is then local-only again. To remove the token too, first take your Tailscale name
out of `PW_ALLOWED_HOSTS`, then delete the `PW_ACCESS_TOKEN` value and restart.

## Alternative: your own HTTPS reverse proxy (advanced)

If you'd rather not use Tailscale, you can put PriceWatch behind a reverse proxy that adds
HTTPS (for example Caddy) **on your home network only**. Run `python3 -m pricewatch set-token`, add the
proxy's hostname to `PW_ALLOWED_HOSTS`, and keep PriceWatch bound to `127.0.0.1` with the
proxy on the same machine. This takes more setup and maintenance, so Tailscale is the
easier safe option for most people.

## Never do this

- **Don't forward a port on your router** to PriceWatch, or otherwise put it on the public
  internet. It's built for you, not for strangers.
- **Don't use plain `http://` with your token** on networks you don't control. The token
  would travel unencrypted.
- **Don't remove the access token** while anything other than your own computer can reach
  PriceWatch, whether through Tailscale, a proxy or a network address. Without it, every
  device that can reach PriceWatch could use it. PriceWatch guards against this: it won't
  start without a token when `PW_HOST` is a network address or `PW_ALLOWED_HOSTS` lists
  another device. It also refuses requests arriving through a proxy when no token is set.
  Don't work around those checks.
