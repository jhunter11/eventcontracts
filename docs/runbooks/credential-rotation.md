# Credential Rotation

Production secrets must come from a secret manager or CI secret store, not from
repo-local files. Local demo files such as `demokey.txt` and `ecmodel.txt` are
gitignored and dockerignored.

## Kalshi Key Rotation

1. Create a new API key in the Kalshi dashboard.
2. Store the key ID as `KALSHI_API_KEY_ID`.
3. Store the private key as `KALSHI_PRIVATE_KEY_PEM`, or mount it and set
   `KALSHI_PRIVATE_KEY_PATH`.
4. Restart paper/live capture or runner processes.
5. Verify with a dry capture against demo before deleting the old key:
   `eventcontracts capture --venue kalshi --transport rest --patterns KX* --out data/key-rotation-check --max-polls 1`.
6. Revoke the old key in the Kalshi dashboard after the new key is observed in
   logs and metrics.

Never bake key files into Docker images. Mount runtime credentials read-only and
rotate immediately if a local key file leaves the operator workstation.
