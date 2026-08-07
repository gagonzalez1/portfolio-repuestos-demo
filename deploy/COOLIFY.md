# Despliegue aislado en Coolify

Esta guía es operativa; no autoriza despliegues, cambios de DNS ni carga de
credenciales. La demo debe vivir en una aplicación y una base nuevas.

## Recursos

- Aplicación sugerida: `portfolio-repuestos-app`.
- PostgreSQL sugerido: `portfolio-repuestos-db`, sin compartir con otros proyectos.
- Primer dominio: temporal, provisto por Coolify y con HTTPS.
- Build: `Dockerfile`; un solo worker mientras los cupos sean contadores en memoria.

## Variables

Configurar como secretos/variables de Coolify, nunca en un `.env` versionado:

```text
APP_ENV=production
CATALOG_BACKEND=demo_postgres
WHATSAPP_ENABLED=false
DATABASE_URL=<URL interna de PostgreSQL>
GEMINI_API_KEY=<secreto>
LLM_MODEL=gemini/gemini-2.5-flash
ADMIN_PASSWORD=<secreto largo y único>
ADMIN_PARAMETROS=<secreto largo y distinto>
DEMO_ENABLED=true
DEMO_MAX_MESSAGES_PER_SESSION=12
DEMO_MAX_MESSAGES_PER_IP_DAY=30
DEMO_GLOBAL_MESSAGES_PER_DAY=500
DEMO_MAX_MESSAGE_LENGTH=1000
DEMO_SESSION_TTL_SECONDS=3600
DEMO_RETENTION_DAYS=7
METAIA_CTA_URL=https://metaia.pro/
```

No configurar tokens Meta ni credenciales WooCommerce. Si el proxy de Coolify
sobrescribe de forma confiable `X-Forwarded-For`, habilitar
`DEMO_TRUST_FORWARDED_FOR=true`; de lo contrario mantenerlo en `false`.

## Validación previa al dominio definitivo

1. Construir localmente: `docker build -t portfolio-repuestos-demo .`.
2. Ejecutar el seed idempotente definido por el backend demo y comprobar conteo/checksum.
3. Verificar `GET /health`, `GET /demo`, inicio anónimo y nueva conversación.
4. Confirmar que `GET /webhook` responde `404` con WhatsApp deshabilitado.
5. Probar límites por sesión, IP y global, y el kill switch.
6. Confirmar que `/admin` y `/admin/params` exigen sus contraseñas separadas.
7. Revisar logs sin prompts, secretos, trazas expuestas al visitante ni llamadas a Meta/Woo.
8. Aprobar manualmente el dominio y el autodeploy; conservar la imagen estable anterior.

Rollback: seleccionar la imagen anterior en Coolify. Los cambios de esquema y
seed deben ser idempotentes; no borrar la base durante un rollback.
