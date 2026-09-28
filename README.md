# milbeerista-en

Publica automáticamente en **@milbeeristablog2** las reseñas del Flickr de pep_tf, traducidas al inglés, empezando por las subidas en enero de 2023. Todo gratis: no usa la API de Flickr (lee sus páginas públicas) y traduce con el plan gratuito de Gemini.

- **Puesta al día:** 5 posts al día (9, 12, 15, 18 y 21 h en verano) mientras la cola vaya por fotos subidas antes de julio de 2025.
- **Después:** baja sola a 3 al día (9, 15 y 21 h) y sigue hasta el presente, incluidas las cervezas nuevas.
- Las fotos de Flickr que no son reseñas (sin #beer ni #craftbeer) se saltan solas.

## Qué hay aquí

- `publicar.py`: busca la siguiente cerveza, traduce la reseña, ajusta la foto a 4:5 y la publica.
- `state.json`: por dónde va la cola (se rellena solo).
- `flickr_ids.json`: lista de fotos de Flickr (se crea sola en la primera ejecución).
- `publicadas.csv`: registro de todo lo publicado (se crea solo).
- `.github/workflows/publicar.yml`: el horario de publicación.
- `.github/workflows/renovar-token.yml`: renueva el token de Instagram cada semana.

## Puesta en marcha (una sola vez)

1. **Crea el repositorio** en GitHub: botón "New", nombre `milbeerista-en`, marcado como **Public** (Instagram necesita poder descargar las fotos; las claves siguen siendo secretas).
2. **Sube los archivos**: "Add file" → "Upload files" y arrastra todo el contenido de la carpeta, incluida la carpeta `.github` (en Mac está oculta: Cmd + Mayús + . en el Finder).
3. **Consigue las claves** (las tres son gratis):
   - `GEMINI_API_KEY`: entra en aistudio.google.com con tu cuenta de Google → "Get API key" → "Create API key". El plan gratuito sobra para 5 traducciones al día.
   - `IG_TOKEN`: el token de Instagram del panel de Meta ("Generar identificador").
   - `GH_PAT`: en GitHub, tu foto de perfil → Settings → Developer settings → Personal access tokens → Fine-grained tokens → "Generate new token". En "Repository access" elige solo `milbeerista-en`; en permisos añade **Secrets: Read and write**; elige la caducidad más larga posible y apúntate la fecha.
4. **Guarda las claves** en el repositorio: Settings → Secrets and variables → Actions → "New repository secret", una por una, con esos nombres exactos. Nunca las escribas en ningún archivo del proyecto.
5. **Prueba sin publicar**: pestaña Actions → activa los workflows si lo pide → "Publicar en Instagram" → "Run workflow" con la casilla de modo prueba marcada. La primera vez tarda unos minutos porque recorre todo el Flickr. En el registro verás la primera cerveza de 2023 y su texto en inglés.
6. **Primera publicación real**: repite el paso 5 con la casilla desmarcada. A partir de ahí, todo funciona solo.

## Cosas a saber

- Si una foto falla 3 veces, se salta. Los errores salen en la pestaña Actions y GitHub te avisa por correo.
- Para cambiar el ritmo, edita las líneas `cron` de `publicar.yml` (en hora UTC). Las fechas de inicio y de fin de la puesta al día son `START_DATE` y `CATCHUP_UNTIL` en `publicar.py`.
- Si Gemini da un error de modelo, cambia `GEMINI_MODEL` en `publicar.py` por el modelo "Flash" que aparezca en AI Studio.
- Como lee las páginas públicas de Flickr en vez de su API, si Flickr cambia el diseño de su web podría dejar de funcionar; el error aparecería en Actions.
- GitHub pausa los horarios si un repositorio pasa 60 días sin cambios. Mientras haya cola no pasa; si algún día se pausa, reactívalo en Actions.
