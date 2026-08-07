# Operación y privacidad de la demo

## Apagado y control de consumo

- Corte inmediato de la experiencia pública: `DEMO_ENABLED=false` y reiniciar la aplicación.
- Corte del agente sin gastar tokens: activar `kill_switch` en `/admin/params`.
  El cache operativo puede tardar hasta 30 segundos en reflejar el cambio.
- WhatsApp permanece fuera de servicio con `WHATSAPP_ENABLED=false`; los webhooks
  devuelven `404` y no se crea cliente HTTP de Meta.
- Los cupos son 12 mensajes por sesión, 30 por IP/día y un máximo global diario
  configurable. Con múltiples workers deben migrarse a PostgreSQL/Redis antes de escalar.

## Privacidad

La demo no pide nombre, email ni teléfono. La IP se transforma en un hash en
memoria para aplicar el cupo diario y no se persiste. Las conversaciones anónimas
usan identificadores aleatorios; al iniciar se purgan registros demo anteriores a
`DEMO_RETENTION_DAYS` (7 días por defecto). Las métricas son contadores agregados
en memoria: visitas, inicios, conversaciones completadas y clics al CTA.

Los paneles `/admin` y `/admin/params` no se enlazan desde la UI pública. Usar
contraseñas distintas, aleatorias y almacenadas solo en Coolify. Rotarlas ante
cualquier sospecha y reiniciar la app para invalidar las sesiones en memoria.

## Revisión rutinaria

- Revisar semanalmente consumo del proveedor LLM y ajustar el cupo global.
- Verificar `/health`; este endpoint no llama al LLM, Meta ni al catálogo.
- Auditar logs y dependencias antes de cada publicación.
- No habilitar WhatsApp, WooCommerce, autodeploy o DNS como parte de una prueba local.
