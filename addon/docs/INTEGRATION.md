# Integrating with existing n8n

Import the JSON workflow templates in `n8n-workflows/`. They use an HTTP Request node to
POST a task to this companion service. Configure the `X-Addon-Token` header in n8n using
a credential, not a literal token in exported workflow JSON.

Suite endpoint:
`POST https://YOUR-SUITE-HOST/api/n8n/jobs`

Example body:
`{"task":"llm_prompt","payload":{"provider_id":"SAVED_PROVIDER_ID","prompt":"Write a short post"}}`

For social publishing:
`{"task":"social_publish","payload":{"account_id":"SAVED_SOCIAL_ACCOUNT_ID","text":"Post text"}}`

The suite runs jobs using its own worker queue. Native n8n workflows and native schedules
continue to run in n8n itself.
