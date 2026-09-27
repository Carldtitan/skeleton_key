# Skeleton Key

**Any web app. Now an API.** Skeleton Key gives your agent a way into the websites you use every day, even the ones that have no API.

**Live:** https://skeletonkey-api.vercel.app · continue as a guest, connect your own account and plug it into your agent.

---

## Problem

Say you want your own agent (Claude Code, Codex or one you built) to use your Luma account for you. It can't: Luma has no API and no MCP. Neither do Facebook Marketplace, your bank, or most of the websites you use.

Today the only way in is a **vision model that clicks through screenshots like a human**. It's slow, expensive and unreliable, because every step is a screenshot and a guess.

## Solution

Skeleton Key turns a website into a **tested API your agent can call**, with no vision needed at run time.

1. **Log in once**, in a live browser sandbox. You type your own password; we never see it.
2. **Explore.** A vision agent walks the site, clicking by numbered element IDs rather than pixel coordinates, and records the requests the site makes behind the scenes.
3. **Generate.** Each distinct request becomes one small operation (`list_my_events`, `rsvp_event`, `get_user_profile`...), granular like `git add` and `git push`, not one script per goal.
4. **Verify.** Every operation is run for real against your live session in an isolated sandbox. Failures go back to the model to be rewritten, up to 4 times, until they pass. Writes are tested as do/undo pairs, and irreversible writes are never run automatically.
5. **Publish** as **MCP**, **REST**, an **OpenAPI spec** and downloadable Python, with personal data redacted from the published docs.

The generator also **learns**: every mistake it fixes becomes a lesson (public on the Skills page), and approved lessons are read before every new site.

### Results

Same model and same logged-in account in every lane; the only difference is whether it browses with vision or calls the Skeleton Key API. 144 graded runs on Luma, each answer checked against the site's real data:

| | Vision browser | With Skeleton Key |
|---|---|---|
| **Faster** (Claude Opus 5, long tasks, median) | 28.4 s | **4.0 s** (7× faster) |
| **Cheaper** (Claude Opus 5, long tasks, per question) | $0.171 | **$0.024** (7× cheaper) |
| **More accurate** (Qwen 3.8, all tasks) | 31 / 36 | **36 / 36** |

Every connection lets you watch the same race live on the **Race** page.

**Generated so far:** Luma (23 operations), Partiful (43) and Cerebral Valley (29), including bearer-token (Firebase) and minute-long-cookie (Clerk) sessions that are kept alive automatically.

## How Vultr was used

Vultr is the whole control plane, not just hosting.

| Vultr product | What it does in Skeleton Key |
|---|---|
| **Cloud Compute VMs** | A **control-plane VM** (FastAPI orchestrator, job engine, MCP and REST gateway) and **two high-performance worker VMs** that run every sandbox. |
| **VPC** | All three machines talk only over a private network. Sandbox ports are bound to the private IP, never the internet. |
| **Serverless Inference** | Every agent call. **Qwen 3.8 (vision)** explores sites from screenshots; **GLM 5.3** writes, repairs and judges the operation code. OpenAI-compatible, at `https://api.vultrinference.com/v1`. |
| **Container Registry** | Holds our two sandbox images: `sk-browser` (Chromium via Playwright, with a live view) and `sk-runner` (executes generated code). Workers pull them with read-only credentials. |
| **Object Storage** | Published APIs (OpenAPI specs, code, READMEs) and the generator's learned skills. |
| **Startup Scripts + Vultr API** | `infra/provision.py` builds the entire stack idempotently: VPC, firewalls, registry, storage and instances. Each machine configures itself on first boot. |
| **Firewall Groups** | Only the control plane is public; workers accept traffic from the VPC alone. |

### Containment

Autonomous agents run untrusted code and drive real accounts, so every run is boxed in:

- **Process isolation.** Everything untrusted runs in Docker sandboxes on the **worker VMs**, never in the app process. Firewall rules on each worker block sandboxes from reaching the private network or the cloud metadata service. Containers drop all Linux capabilities and can't gain privileges.
- **Secret hygiene.** No API or inference keys ever enter a sandbox; they stay on the control plane. Generated code never contains tokens: the user's session is injected per call.
- **Resource limits.** Browser sandboxes are capped at 2 GB and 1.5 CPUs; code runs at 512 MB, 1 CPU and 128 processes, with a 90-second timeout.
- **Lifecycle discipline.** Every code run gets a fresh container that's destroyed immediately afterwards. Exploration, race and check browsers are destroyed when the job ends, and guest connections self-destruct after 6 hours.
- **Humans in the loop.** Logins, CAPTCHAs and expired sessions are handed to a person through a live browser; irreversible actions are flagged as destructive so the agent asks before using them.

## How to use it

### Try it as a guest (no account)

1. Open https://skeletonkey-api.vercel.app and press **Continue as guest** (4 guest seats at a time).
2. Open a site (**Sites**), press **Connect your ... account**, log in inside the live browser and press **Done**.
3. Copy your connection into your agent:

```bash
# Claude Code
claude mcp add --transport http luma https://skeletonkey-api.vercel.app/mcp/<your-key>

# Codex
codex mcp add luma --url https://skeletonkey-api.vercel.app/mcp/<your-key>

# Any HTTP client
curl -X POST https://skeletonkey-api.vercel.app/v1/luma.com/list_my_events \
  -H "Authorization: Bearer <your-key>" -H "Content-Type: application/json" -d '{"period":"future"}'
```

Guest connections last 6 hours; after that the browser is destroyed and the key stops working.

4. Or open **Race**, pick a task and watch four agents do it with and without Skeleton Key.

### Public docs

- OpenAPI spec: `https://skeletonkey-api.vercel.app/specs/<domain>/openapi.json`
- Python code: `https://skeletonkey-api.vercel.app/specs/<domain>/download.zip`
- What the generator has learned: https://skeletonkey-api.vercel.app/#/skills

### Run your own

```bash
cp .env.example .env            # Vultr API key, inference key, admin password
python infra/provision.py       # VPC, firewalls, registry, storage, 3 VMs
# then deploy control/ to the control VM and worker/ to each worker (docker compose up)
```

Admins sign in on the same page, paste a login URL into **Generate**, log in once in the live browser, and get a verified API about an hour later.

### Repo map

| Path | What |
|---|---|
| `control/app/` | Orchestrator: explorer, generator and verifier, publisher, MCP and REST gateway, race, guests |
| `worker/` | Worker daemon that starts, caps and destroys sandboxes |
| `sandbox/` | `sk-browser` and `sk-runner` images |
| `infra/provision.py` | The Vultr stack, as code |
| `web/` | The dashboard (Home, Sites, Skills, Race) |
