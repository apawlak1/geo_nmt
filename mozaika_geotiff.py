# -*- coding: utf-8 -*-
'''
MOZAIKA JEDNOCZASOWA JAKO POJEDYNCZY GEOTIFF (alternatywa dla zapisu do ZARR)

Czesciowe przywrocenie funkcjonalnosci z Twojego pierwotnego kodu (mozaika = scalony raster zapisany jako
GeoTIFF, budowany merge(method='first', nearest) - najnowsze arkusze nadpisuja starsze). W odroznieniu od
oryginalu: bez generalizacji NFP i bez oceny bledow - to jest odpowiednik jednej warstwy bazowej z ZARR,
zapisany jako zwykly plik .tif zamiast do store'u. Bez wymiaru czasu i bez poziomow pochodnych - kazdy rok
to osobny, niezalezny plik.

Buduje sie BLOKAMI i zapisuje od razu do otwartego pliku (rasterio Window), wiec pamiec nie zalezy od
wielkosci powiatu - tak samo jak przy zapisie do ZARR.
'''

import logging
from pathlib import Path

import numpy as np
import rasterio
import rasterio.windows
from rasterio.crs import CRS
from shapely.geometry import box, shape

from nfp_mosaics_zarr import _zbuduj_blok, siatka_z_geometrii

log = logging.getLogger('geo_nmt')

ROZMIAR_KAFLA_TIF = 256   #wewnetrzny rozmiar kafla GeoTIFF (blockxsize/blockysize); niezalezny od blok_px


def zbuduj_mozaike_geotiff(kafle_najnowsze_pierwsze, geometria, output_path, cellsize=None, blok_px=2048):
    '''
    kafle_najnowsze_pierwsze : lista sciezek GeoTIFF (EPSG:2180), POSORTOWANA najnowsze->najstarsze
                               (jak w nfp_mosaics_zarr.zbuduj_warstwe_bazowa)
    geometria                : granica obszaru (shapely) w EPSG:2180
    cellsize                 : rozdzielczosc mozaiki [m]; None = najdrobniejszy piksel wsrod kafli
    zwraca (output_path, cellsize)
    '''
    if not kafle_najnowsze_pierwsze:
        raise ValueError('brak kafli do zbudowania mozaiki')

    geom_shape = geometria if hasattr(geometria, 'bounds') else shape(geometria)
    if cellsize:
        cellsize = float(cellsize)
    else:
        #ZGODNIE Z ZARR: auto = NAJDROBNIEJSZY piksel wsrod kafli (nie najgrubszy) - ta sama zasada co przy
        #tworzeniu bazy ZARR (patrz zarr_aktualizacja.py), zeby oba formaty wyjsciowe przy 'rozdzielczosc': null
        #dawaly ta sama rozdzielczosc
        rozdzielczosci = []
        for p in kafle_najnowsze_pierwsze:
            with rasterio.open(p) as src:
                rozdzielczosci.append(abs(src.res[0]))
        cellsize = min(rozdzielczosci)

    siatka = siatka_z_geometrii(geom_shape, cellsize, faktory=())
    with rasterio.open(kafle_najnowsze_pierwsze[0]) as pierwszy:
        crs = pierwszy.crs or CRS.from_epsg(siatka.epsg)

    zrodla_bounds = []
    for p in kafle_najnowsze_pierwsze:
        with rasterio.open(p) as src:
            zrodla_bounds.append((p, src.bounds))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    blok_tif = min(ROZMIAR_KAFLA_TIF, siatka.width, siatka.height)
    blok_tif = (blok_tif // 16) * 16          #GeoTIFF: rozmiar kafla musi byc wielokrotnoscia 16
    tiled = blok_tif >= 16

    with rasterio.open(
        output_path, 'w', driver='GTiff', height=siatka.height, width=siatka.width, count=1,
        dtype='float32', crs=crs, transform=siatka.transform, nodata=np.nan, compress='lzw',
        **({'tiled': True, 'blockxsize': blok_tif, 'blockysize': blok_tif} if tiled else {})
    ) as dst:
        zapisane = 0
        for y0, y1, x0, x1 in siatka.okna(blok_px):
            okno = rasterio.windows.Window(x0, y0, x1 - x0, y1 - y0)
            win_bounds = siatka.okno_bounds(y0, y1, x0, x1)

            if geom_shape.intersects(box(*win_bounds)):
                blok = _zbuduj_blok(zrodla_bounds, win_bounds, okno, siatka.cellsize, geom_shape, np.nan)
            else:
                blok = None

            if blok is None:
                dst.write(np.full((y1 - y0, x1 - x0), np.nan, dtype='float32'), 1, window=okno)
            else:
                dst.write(np.round(blok, 2).astype('float32'), 1, window=okno)
                zapisane += 1

    log.info('Mozaika GeoTIFF (%s m, %s blokow z danymi): %s', cellsize, zapisane, output_path)
    return output_path, cellsize
