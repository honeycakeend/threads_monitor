# Meta App Review deployment notes

Internal setup notes for publishing the legal pages and configuring the Meta app.

## Publish legal pages with GitHub Pages

1. Open the [repository Pages settings](https://github.com/honeycakeend/threads_monitor/settings/pages).
2. Under **Build and deployment**, select **Deploy from a branch**.
3. Select branch **main** and folder **/docs**, then click **Save**.
4. Wait for deployment to finish.
5. Verify that the following pages open without authentication:
   - https://honeycakeend.github.io/threads_monitor/privacy-policy.html
   - https://honeycakeend.github.io/threads_monitor/terms.html
   - https://honeycakeend.github.io/threads_monitor/data-deletion.html

## Configure the Meta app

Open [Meta App Dashboard](https://developers.facebook.com/apps/), select the app, and enter:

- **Privacy Policy URL:** `https://honeycakeend.github.io/threads_monitor/privacy-policy.html`
- **Terms of Service URL:** `https://honeycakeend.github.io/threads_monitor/terms.html`
- **User Data Deletion / Data Deletion Instructions URL:** `https://honeycakeend.github.io/threads_monitor/data-deletion.html`

Save the settings only after all URLs are publicly accessible.
