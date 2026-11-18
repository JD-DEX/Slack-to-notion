## Send to Notion in Slack (Simplified OSS Edition)

This is a simplified open-source version of [Send to Notion in Slack](https://seratch.notion.site/Send-to-Notion-in-Slack-2f7fd79ee4e64ec5b8053227f548df78).
The benefits of this light-weight version are:
* You can run this app inside your managed infrastructure
* You can take full control of the app, from data management to code customization
* You can customize any of the metadata of the app such as its name, icon, and description

As this is a bit simpler implementation than the hosted one, there are a few limitations:
* You have to set up both Slack and Notion integrations on your own
* This app connects a pair of a single Slack Org/Workspace and a single Notion Workspace
* This app is supposed to be installed once by your Notion Workspace admin

### This app is free! but your support would be appreciated ❤️

Anyone can use and modify this app under the MIT license, but [buying me a coffee](https://www.buymeacoffee.com/seratchoss) would be greatly appreciated!

## Steps to configure this app

To enable this app, you will go through the following steps:
* Preparation on the Slack side:
  * Create a new Slack app settings
  * Install the app into your Slack Workspace
  * Create a new app-level token for the Socket Mode connection
* Preparation on the Notion side:
  * Create new integration settings
  * Share your databases with the app
* Deploy this app to somewhere you manage
  * Build a Docker image using the Dockerfile
  * Spin up a new running app with the image

### Preparation on the Slack side

To set up your Slack app configurations, go through the following three steps.

#### Create a new Slack app settings

* Head to https://api.slack.com/apps
* Click the "Create New App" button
* Select "From an app manifest"
* Select the Slack workspace to use this app, and then click "Next"
* Paste the whole content in `./app-manifest.json` in this repo, and then click "Next"

You will see the top page of the created app configuration page!

#### Install the app into your Slack Workspace

* Head to **Settings** > **Install App** on `https://api.slack.com/apps/{your app id}`
* Click "Install to Workspace" and complete the OAuth flow
* Save the **Bot User OAuth Token** string value. You will use it as "SLACK_BOT_TOKEN" env variable later

#### Create a new app-level token for the Socket Mode connection

* Head to **Settings** > **Basic Information** on `https://api.slack.com/apps/{your app id}`
* Scroll down to **App-Level Tokens** section
* Click "Generate Token and Scopes"
* Give "socket mode" (or whatever) for Token Name
* Click "Add Scope" and select `connections:write`
* Click "Generate" to generate a new token
* Save the string value. You will use it as "SLACK_APP_TOKEN" env variable later

### Preparation on the Notion side

On the Notion side, all you need to do are to create a new integration and share your databases with the integration.

#### Create new integration settings

* Head to https://www.notion.so/my-integrations
* Click "New integration" button to start
* Give "Send to Notion in Slack" (or whatever you like) for "Name"
* "Logo" is optional, but you can use the `./icon.png` file in this repo
* Double-check that the "Associated workspace" is correct
* This app needs only "Read Content" and "Insert Content" capabilities
* This app does not need any user capabilities so that you can go with "No user information" for it
* Click "Submit" button to complete
* Save the "Internal Integration Token" string value. You will use it as "NOTION_API_TOKEN" env variable later

#### Share your databases with the app

* Head to the associated Notion Workspace
* Click the menu of a database page
* Click "Add connections" in the "Connections" section
* Add the newly created integration
* Repeat this step for all the databases that you want to send data from Slack

### Deploy this app to somewhere you manage

Build the image from this fork. Do not use the upstream pre-built image
(`ghcr.io/seratch/send-to-notion-in-slack-oss`): it does not have the access control and the other fixes in this fork.

#### Environment variables

| Variable | Required | Description |
|-|-|-|
| `SLACK_BOT_TOKEN` | Yes | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | App-level token with `connections:write` (`xapp-...`) |
| `NOTION_API_TOKEN` | Yes | Notion internal integration token |
| `ALLOWED_SLACK_USER_IDS` | Yes | Comma-separated Slack user IDs that can use the app. The app does not start if this is empty. |
| `LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Default: `INFO`. |

Copy `.env.example` to `.env` for local runs. `.env` files are ignored by git and by Docker. Never commit them.

To find a Slack user ID, open the user's profile in Slack, click the "more" menu and select "Copy member ID".

#### Build a Docker image using the Dockerfile

```bash
docker build . -t send-to-notion
```

The image runs as a non-root user (UID 10001) and installs only hash-checked dependencies from `requirements.txt`.

#### Spin up a new running app with the image

```bash
cp .env.example .env   # then edit .env and set real values
docker run --env-file .env send-to-notion
```

The app uses Socket Mode. It opens an outbound WebSocket connection to Slack and does not need an open port.

### Deploy on Railway

1. Create a new service from this repository. Railway finds the `Dockerfile` in the repository root and builds the image from it.
2. In the service **Variables** tab, set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `NOTION_API_TOKEN`, `ALLOWED_SLACK_USER_IDS` and `LOG_LEVEL` (use `INFO`).
3. In **Settings > Networking**, do not generate a public domain. The app is a worker. It has no HTTP server.
4. In **Settings**, keep **Serverless** (formerly "App Sleeping") off. Slack cannot wake a sleeping Socket Mode app, so a sleeping service misses all requests.
5. Deploy. The logs show `Bolt app is running!` when the app is connected.

If `ALLOWED_SLACK_USER_IDS` is missing, the deploy fails at startup with a clear error. This is intentional.

### Update dependencies

Edit `requirements.in`, then regenerate the hashed lockfile:

```bash
uv pip compile --universal --python-version 3.13 --generate-hashes requirements.in -o requirements.txt
```

### Container image on GHCR

The workflow in `.github/workflows/docker-image.yml` publishes an image to GHCR on each push to `main`.
Set that GHCR package to **private** in its package settings.

### Notion API version and data sources

This app uses Notion API version `2025-09-03`. In this version a database can hold more than one data source.
The database picker lists data sources, not databases. A database with two data sources shows two entries,
and you pick the one to write to. The picker shows only the data source title, so two data sources with the
same title look the same. Give data sources distinct titles to avoid confusion.

## How to run the app

There are three ways to open the modal dialog to send data to Notion.

* Use the `/send-to-notion` slash command
* Run the global shortcut labeled "Submit data to Notion"
* Click the "Submit data to Notion" button on the app's Home tab

Please refer to [the product page](https://seratch.notion.site/Send-to-Notion-in-Slack-2f7fd79ee4e64ec5b8053227f548df78) for more details!

### This app is free! but your support would be appreciated ❤️

Anyone can use and modify this app under the MIT license, but [buying me a coffee](https://www.buymeacoffee.com/seratchoss) would be greatly appreciated!
