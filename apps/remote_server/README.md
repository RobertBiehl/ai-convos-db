# convos-remote-server

Self-hosted opaque relay for Convos personal and team synchronization; it
stores encrypted events and synchronization metadata without workspace keys or
conversation plaintext.

```bash
uv tool install convos-remote-server
convos-server serve --db /path/to/relay.db
```

See the [complete self-hosting documentation](https://github.com/RobertBiehl/convos/blob/master/docs/remote.md).
