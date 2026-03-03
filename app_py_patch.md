# Patch: Add Beta Discovery Mode to app.py

Two changes only. Everything else stays exactly as-is.

---

## CHANGE 1 — Import (add after your existing imports, around line 14)

```python
from beta_discovery import is_beta_user, handle_beta_message
```

---

## CHANGE 2 — Webhook hook (add between the supplier block and the option-selection block)

Find this comment in your webhook():
```python
    # 3. CUSTOMER SELECTING AN OPTION
```

Insert this block **directly above it**:

```python
    # 2b. BETA DISCOVERY MODE → whitelisted numbers get open-ended Zeli flow
    if is_beta_user(incoming_number):
        thread = threading.Thread(
            target=handle_beta_message,
            args=(incoming_number, incoming_message)
        )
        thread.daemon = True
        thread.start()
        return str(response)

```

---

## CHANGE 3 — Railway environment variable

Add to your Railway environment:

```
BETA_WHITELIST_NUMBERS=+507XXXXXXXX,+507YYYYYYYY
```

Comma-separated, international format. No spaces.
To add a number without redeploying, just update the env var and Railway
will pick it up on next request (no restart needed for env reads at
call-time, since `get_beta_whitelist()` reads `os.getenv` fresh each time).

---

## What each beta user experiences

1. First message → Claude greets them as Zeli, starts listening naturally
2. Follow-up messages → Claude asks 1 clarifying question at a time
3. Once problem is clear → Claude summarises, says Zeli is on it, warm close
4. You get an instant WhatsApp alert with the structured signal summary + transcript

## What you get as owner

Instant WhatsApp message formatted like:

```
🧪 Nueva señal beta capturada
Número: +507...

📋 Problema: busca proveedor de materiales de construcción sin ir a la ciudad
🔁 Frecuencia: cada 2-3 semanas
🛠️ Solución actual: viaja a Panama City o llama a contactos personales
🌡️ Nivel de dolor: alto
📂 Categoría potencial: materiales construcción / B2B procurement

💬 Conversación reciente:
👤 Usuario: necesito conseguir cemento y varillas sin ir hasta la ciudad
🤖 Zeli: Entiendo, ¿con qué frecuencia necesitas conseguir este tipo de materiales?
...
```

Plus a structured log entry via your existing `log_request()` logger.
