# Legal pages for Meta App Review

Static pages to host publicly (HTTPS) and paste into Meta App Dashboard.

| File | Meta field |
|------|------------|
| `privacy-policy.html` | Privacy Policy URL |
| `data-deletion.html` | Data Deletion Instructions URL |
| `index.html` | Optional landing page |

## Before publishing

1. Contact email is set to `honeycakeend@gmail.com`.
2. Optionally rename “Threads Monitor Bot” if your Meta app display name differs.

## Host on GitHub Pages (fastest)

1. Push this repo to GitHub (do **not** commit `.env`).
2. Repo → **Settings → Pages**:
   - Source: Deploy from a branch
   - Branch: `main` (or `master`)
   - Folder: `/docs`
3. After deploy, URLs will look like:

```text
https://YOUR_GITHUB_USER.github.io/REPO_NAME/
https://YOUR_GITHUB_USER.github.io/REPO_NAME/privacy-policy.html
https://YOUR_GITHUB_USER.github.io/REPO_NAME/data-deletion.html
```

4. Open both URLs in a private window — they must load without login.

## Paste into Meta

1. [developers.facebook.com](https://developers.facebook.com/apps/) → your app
2. **Settings → Basic**
3. **Privacy Policy URL** → `.../privacy-policy.html`
4. **User data deletion** / Data Deletion Instructions URL → `.../data-deletion.html`
5. Save changes

## Local preview

```bash
cd docs
python -m http.server 8080
# open http://127.0.0.1:8080/
```
