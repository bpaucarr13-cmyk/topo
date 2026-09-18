# TopoFast

Plugin de QGIS que automatiza el flujo: seleccionar un área en el mapa -> descargar el DEM correspondiente desde [OpenTopography](https://opentopography.org/) -> extraer las curvas de nivel (GDAL) -> cargar ambas capas en el proyecto. Todo con un solo clic + arrastre sobre el mapa.

## Requisitos

- QGIS 4.2.2 (también funciona en QGIS 3.28+).
- Conexión a internet.
- Una **API Key gratuita de OpenTopography**: registrate en https://portal.opentopography.org/myopentopo y copiá la key.

## Instalación

### Opción A: desde el .zip (recomendada para instalar en otra PC)

1. Generá el paquete (o usá el `.zip` ya generado, ver más abajo) y en QGIS andá a **Complementos > Administrar e instalar complementos > Instalar a partir de ZIP**.
2. Elegí el archivo `topofast_vX.X.zip` y presioná **Instalar complemento**.
3. QGIS lo activa automáticamente. Si no, andá a la pestaña "Instalados" y activá **TopoFast**.

### Opción B: copiando la carpeta a mano

1. Copiá toda la carpeta `topofast` dentro de la carpeta de plugins de tu perfil de QGIS:
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\` (o `QGIS4` según la carpeta que use tu instalación — fijate cuál existe en tu equipo).
   - Se puede ver/editar la ruta exacta desde QGIS: menú **Configuración > Perfiles de usuario > Abrir carpeta de perfil activo**, y de ahí entrar a `python/plugins`.
2. Reiniciá QGIS (o recargá plugins con el complemento "Plugin Reloader").
3. Andá a **Complementos > Administrar e instalar complementos**, pestaña "Instalados", y activá **TopoFast**.

### Armar el .zip para distribuir

Desde PowerShell, parado en la carpeta que CONTIENE la carpeta `topofast` (no adentro de ella):

```powershell
Compress-Archive -Path "topofast" -DestinationPath "topofast_v0.16.zip" -Force
```

El .zip tiene que contener la carpeta `topofast/` en su raíz (no los archivos sueltos) — así es como QGIS espera el paquete tanto para "Instalar a partir de ZIP" como para subirlo al repositorio oficial de plugins.

## Uso

1. Hacé clic en el ícono de TopoFast (barra de herramientas o menú Complementos > TopoFast).
2. Completá el diálogo:
   - **API Key** de OpenTopography.
   - **Modelo de elevación** (por defecto SRTM GL3, 90 m).
   - **Equidistancia de curvas** en metros (por defecto 10 m).
   - **Carpeta de salida** (opcional; si se deja vacía usa una carpeta temporal).
3. Presioná **OK** y dibujá un rectángulo sobre el área de interés (clic, arrastrar, soltar).
4. El plugin descarga el DEM, lo carga como capa ráster, extrae las curvas de nivel con `gdal:contour` y las carga como capa vectorial (`topofast_curvas_nivel.gpkg`).

## Manejo de vacíos de datos (DEM voids)

En zonas de nieve/glaciares (p. ej. Cordillera Blanca) los DEM globales suelen tener píxeles sin dato. TopoFast:

1. Reetiqueta explícitamente el valor de vacíos (`-9999`) como NoData en el GeoTIFF descargado, para que tanto el color del ráster como las curvas de nivel lo ignoren correctamente (antes se veía todo negro y aparecía una curva falsa gigante en el borde del vacío).
2. Si tildás **"Rellenar vacíos del DEM"** (activado por defecto), corre `gdal:fillnodata` para interpolar esos huecos antes de generar las curvas, evitando cortes en las líneas.
3. Si el área tiene más de ~10% de vacíos, muestra un aviso sugiriendo cambiar a **Copernicus GLO-30 (COP30)**, que es un modelo moderno prácticamente sin vacíos — es la opción recomendada por defecto.
4. El DEM se carga con una paleta de colores por elevación (verde-amarillo-marrón-blanco) en vez del render en escala de grises por defecto.

## Selecciones sucesivas

Cada clic en "Generar curvas de nivel" es una corrida independiente y **se acumula**: el DEM y las curvas de la corrida anterior se quedan en el proyecto, y se agregan capas nuevas ("TopoFast DEM 2", "Curvas de nivel 2", etc.) para la nueva selección. Cada corrida escribe sus propios archivos con un sufijo único (`topofast_dem_<id>.tif`, `topofast_curvas_nivel_<id>.gpkg`...) en vez de reutilizar siempre el mismo nombre — así Windows nunca bloquea el archivo de la corrida anterior (que sigue abierto como capa) y cada selección se ve reflejada correctamente. Si no querés acumular capas, borralas manualmente del panel "Capas" entre una corrida y otra.

## Exportar a CAD (DXF)

Tildá **"Exportar también a DXF (CAD)"** para que, además de las capas en QGIS, se genere un archivo `.dxf` listo para abrir en AutoCAD/Civil3D:

- **3D (recomendado)**: cada curva de nivel se escribe como una polilínea 3D real, con la elevación del DEM en cada vértice (no solo como atributo). Es el modo correcto si después vas a usar las curvas para armar una superficie/TIN en Civil3D o simplemente querés verlas en su altura real.
- **2D**: las curvas se escriben planas (sin altura), solo como planta.

Importante: si en el CAD generás una **superficie/TIN a partir de estas curvas**, usalas como *líneas de quiebre (breaklines)*, no como una nube de puntos suelta — si se triangula todos los vértices de todas las curvas sin respetarlas como quiebres, se arma una malla cruzada y desordenada en vez de un terreno limpio (eso es lo que pasaba antes, con curvas planas en 2D, subidas después a "3D" mezclando vértices de curvas distintas).

### Proyección del DXF (UTM automático)

Las curvas en QGIS quedan en EPSG:4326 (grados), pero un DXF en grados es inútil para medir en CAD: las distancias y áreas no representan metros reales. Por eso, al exportar, TopoFast calcula automáticamente la zona UTM que corresponde al centro del área seleccionada (por ejemplo, `UTM 18S / EPSG:32718` para la zona de Huaraz/Cordillera Blanca) y reproyecta las curvas a esa zona antes de escribir el DXF — X/Y quedan en metros, Z no se toca. La zona usada se muestra en el mensaje de éxito al terminar.

## Notas y límites conocidos (v0.32)

- La descarga sigue siendo síncrona (la interfaz de QGIS queda ocupada mientras baja el DEM), pero ahora tiene **timeout** (120 s), progreso real cuando el servidor lo informa, y botón **Cancelar** — antes, si OpenTopography no respondía, no había forma de salir sin forzar el cierre de QGIS.
- OpenTopography limita el tamaño de área por request según el tipo de DEM; si el área es muy grande la API devuelve error.
- Cada corrida genera archivos con sufijo único, pero **los archivos intermedios que no quedan como capa** (DEM crudo, DEM sin recortar/sin rellenar si quedó superado por un paso siguiente, ráster de pendiente, DEM UTM temporal del LandXML) **se borran automáticamente** al terminar cada corrida. Solo quedan en la carpeta de salida el DEM final, las curvas, el informe de texto y los archivos exportados (DXF/LandXML).
- Los parámetros (API Key, DEM, equidistancia, relleno de vacíos, carpeta) quedan guardados entre sesiones.

## Estructura del proyecto

```
topofast/
├── __init__.py          # punto de entrada (classFactory)
├── metadata.txt          # metadatos del plugin
├── topofast.py            # lógica principal (descarga DEM + curvas de nivel)
├── topofast_dialog.py      # diálogo de parámetros
├── map_tool_extent.py       # herramienta de selección de área en el canvas
└── README.md
```
