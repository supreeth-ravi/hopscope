# Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "AWS sign-in needed" | Your session expired. Run `aws login` (or `aws sso login --profile …`) and run again. |
| `warning: … skipped (AccessDenied …)` | The role lacks one read permission; that view will be empty. Compare with [`iam-policy.json`](../skills/hopscope/references/iam-policy.json). |
| Everything lands in one system, or nothing is linked | The repos you passed aren't the ones deployed. Check which repo builds each image, then pass those repos or set `placement` in `hopscope.json`. |
| A trace runs on the wrong container | Fix that service under `placement` in `hopscope.json` and re-run. |
| Charts are empty | No public load balancer, or no CloudWatch data in the last 72 hours. The other views still work. |
| No voice in the trace | Your browser has no speech voices. On macOS, add one in System Settings → Accessibility → Spoken Content. |
| The page says the data didn't load | A data file is missing; run `hopscope.py all` again, then `hopscope.py build`. |
