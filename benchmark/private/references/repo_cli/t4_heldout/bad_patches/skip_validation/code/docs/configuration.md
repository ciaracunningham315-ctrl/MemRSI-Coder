# Configuration contract

For each deploy option independently, resolve command line, `REPOCLI_DEPLOY_<OPTION>`, `.repo-cli.json` project entry, `$HOME/.config/repo-cli/config.json` user entry, and default in that order. `deploy --workers N --timeout N` accepts workers 1..16 and timeout 5..120 inclusive; values must be ASCII decimal integers. Resolve and validate both options before writing one record. A selected invalid higher-priority value is an error and must not fall through. An explicit override of one field does not replace the other field's source chain.
