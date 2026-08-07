# MotorIA: agente demostrativo para búsqueda de repuestos

> Proyecto demostrativo desarrollado a partir de una necesidad relevada en el
> rubro de repuestos. El catálogo, los precios y la disponibilidad son ficticios.
> No representa una tienda activa ni una relación comercial vigente con una
> empresa específica.

## El problema

Buscar un repuesto no suele empezar con un código exacto. Las personas escriben
con jerga, recuerdan solo el modelo, mezclan cilindrada y motor o aportan una
medida incompleta. Un buscador literal devuelve ruido o ningún resultado.

## La solución

MotorIA conduce una conversación breve: interpreta la pieza y sus equivalencias,
reconoce vehículo, motor y medida, pide el dato que falta cuando hay ambigüedad y
presenta coincidencias de un catálogo demostrativo anonimizado. No vende, reserva
ni deriva a un comercio; el siguiente paso es un CTA de contacto con MetaIA.

## Capacidades y decisiones

- Consultas en lenguaje cotidiano y jerga del rubro.
- Búsqueda por repuesto, modelo, motor, cilindrada y medida normalizada.
- Preguntas de aclaración y manejo explícito de casos sin resultado.
- FastAPI, PostgreSQL, agente LLM y adaptador de catálogo propio.
- Acceso anónimo sin PII, retención corta y paneles privados.
- Límites por sesión/IP/día, cupo global y doble kill switch operativo.
- Canal Meta y WooCommerce fuera del runtime de portfolio.

## Presentación

La publicación debe acompañarse con 3–5 capturas: portada y aviso, una consulta
directa, una aclaración por ambigüedad, un resultado y el CTA. Un video opcional
de 30–45 segundos puede recorrer esos mismos pasos sin mostrar paneles, secretos,
logs ni datos reales.

CTA sugerido: **¿Querés aplicar un agente especializado a tu operación? Hablemos
con MetaIA.**
