# Social platform requirements (official APIs only)

Each module is built from the platform's public documentation. **Live publishing has not been tested** (no credentials were available); requests are verified against mocks only.

| Platform | Credentials to enter | Scopes / permissions | Media | Notes |
|---|---|---|---|---|
| Reddit | client_id, client_secret, refresh_token (or access_token) | `identity`, `submit` | link posts | Needs a Reddit app; subreddit rules/ratelimits apply (`RATELIMIT` -> retried). |
| Facebook | page_id, page_access_token | `pages_manage_posts`, `pages_read_engagement` | photo URL, video URL | Native scheduling via `scheduled_publish_time` (10 min-30 days). App review needed beyond app admins. |
| Instagram | ig_user_id, access_token | `instagram_basic`, `instagram_content_publish` | image/Reels (public URL) | Business/Creator account linked to a Page. Text-only posts are impossible. |
| YouTube | client_id, client_secret, refresh_token | `youtube.upload` | video (resumable upload) | ~1600 quota units per upload of the 10,000 daily default; unverified API projects force private videos. |
| X | access_token (OAuth 2.0 *user* token); optional refresh_token + client_id/secret | `tweet.read tweet.write users.read media.write offline.access` | images | App-only bearer tokens cannot post. API access is paid. The v2 media-upload request shape is the least verified part. |
| LinkedIn | access_token, author_urn | `w_member_social` (or `w_organization_social`), `openid profile` for the test | single image | Uses `/rest/posts` with a `LinkedIn-Version` header (default 202510, configurable per account as `api_version`; LinkedIn sunsets old versions). |
| Threads | user_id, access_token | `threads_basic`, `threads_content_publish` | image, video | Media containers are polled until ready. |
| Pinterest | access_token, board_id | `boards:read pins:read pins:write` | image URL | Trial access is sandbox only. Video pins are not implemented. |

Scheduling: use the add-on **Schedules** page (any platform) - or Facebook's native scheduling field.
Rate limits: HTTP 429/5xx and network errors are retried with backoff, honouring `Retry-After`.
Not implemented (documented honestly): Reddit image/video upload, X video, Pinterest video, LinkedIn video/multi-image, Facebook/Instagram carousels.
