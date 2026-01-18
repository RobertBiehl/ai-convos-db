---
summary: "ChatGPT integration: API endpoints, export formats, and cookie auth."
read_when:
  - Fetching ChatGPT conversations
  - Debugging ChatGPT API issues
  - Importing ChatGPT exports
  - Understanding ChatGPT data structure
---

# ChatGPT Integration

Supports both web API fetching and export file parsing.

## Web API Fetching

Uses browser cookies to authenticate with ChatGPT's backend API.

### Endpoints

**List conversations:**
```
GET https://chat.openai.com/backend-api/conversations?offset=0&limit=100
```

Response:
```json
{
  "items": [
    {
      "id": "uuid",
      "title": "Chat Title",
      "create_time": 1704067200.0,
      "update_time": 1704067200.0,
      "model": "gpt-4"
    }
  ],
  "total": 150,
  "limit": 100,
  "offset": 0
}
```

**Get conversation detail:**
```
GET https://chat.openai.com/backend-api/conversation/{id}
```

Response:
```json
{
  "mapping": {
    "node-id": {
      "message": {
        "author": {"role": "user"},
        "content": {"parts": ["Hello"]},
        "create_time": 1704067200.0,
        "metadata": {"model_slug": "gpt-4"}
      }
    }
  }
}
```

### Required Headers

```python
headers = {
    "Origin": "https://chat.openai.com",
    "Referer": "https://chat.openai.com/",
    "User-Agent": "Mozilla/5.0 ..."
}
```

### Cookie Requirements

Must have valid session cookies from chat.openai.com:
- `__Secure-next-auth.session-token`
- `cf_clearance`

## Export File Parsing

ChatGPT exports are ZIP files containing `conversations.json`.

### Export Format

```json
[
  {
    "id": "conv-uuid",
    "title": "Chat Title",
    "create_time": 1704067200.0,
    "update_time": 1704067200.0,
    "default_model_slug": "gpt-4",
    "gizmo_id": "g-abc123",  // custom GPT ID if used
    "mapping": {
      "node-id": {
        "message": {
          "author": {"role": "user"},
          "content": {
            "content_type": "text",
            "parts": ["Message text"]
          },
          "metadata": {}
        }
      }
    }
  }
]
```

### Content Types

Messages can contain different content types in `parts`:

- **Text**: `{"parts": ["string content"]}`
- **Image**: `{"parts": [{"content_type": "image_asset_pointer", "asset_pointer": "file://...", "name": "image.png"}]}`
- **File**: `{"parts": [{"content_type": "file", "name": "doc.pdf", "size": 1024}]}`

### Tool Calls

Plugin/tool usage appears in message metadata:

```json
{
  "metadata": {
    "invoked_plugin": {
      "namespace": "browser",
      "type": "tool"
    },
    "args": {"query": "search term"}
  }
}
```

## Usage

```bash
# Sync from web API (auto-tries browsers)
uv run convos sync

# Import export files via env
CONVOS_IMPORT_PATHS="~/Downloads/chatgpt-export.zip,~/Downloads/conversations.json" uv run convos sync
```

## Troubleshooting

**403 Forbidden:**
- Cookies may be expired - log into chat.openai.com in browser
- `sync` will try Safari and Chrome

**Empty response:**
- Account may have no conversations
- Check total count in API response

**Missing messages:**
- Deleted messages don't appear in export
- Some system messages are filtered
