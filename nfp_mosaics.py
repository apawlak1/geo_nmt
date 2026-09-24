# -*- coding: utf-8 -*-
'''
FUNKCJE POMOCNICZE DO KAFLI (z dawnego nfp_mosaics.py)

Z nfp_mosaics.py zostaly TYLKO te funkcje, z ktorych korzysta ZARR i reszta narzedzia:
  find_date, find_native_cellsize, find_min_cellsize, wyrownanie_do_siatki, kafle_do_pokrycia
(kod niezmieniony). Generalizacja metodami NFP (resampling bazy, ujednolicanie,
referencja natywna, generate_nfp_mosaics) zostala usunieta z tego repo - zostaje w repo z magisterki.
Nazwa pliku zostala, zeby nie zmieniac importow w nfp_mosaics_zarr.py / mapa_kafli.py.
'''

import math
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.merge import merge
from rasterio.enums import Resampling
from rasterio.features import geometry_mask


def find_date(tif_path, mapa_daty):
    #---TAG WPISANY PRZEZ processor.py W MOMENCIE KONWERSJI---
    try:
        with rasterio.open(tif_path) as src:
            tag=src.tags().get('akt_data')
        if tag:
            return pd.Timestamp(tag)
    except Exception:
        pass  #plik nieczytelny - lecimy do fallbacku ponizej

    #---BRAK TAGU: SCHODZI DO NAJSTARSZEGO---
    print(f'[UWAGA] Brak tagu daty w pliku "{Path(tif_path).name}": '
          f'traktowany jako NAJSTARSZY (Timestamp.min).')
    return pd.Timestamp.min


def find_native_cellsize(tiffs):
    #---NAJGRUBSZY (NAJWIEKSZY) PIKSEL WSROD PODANYCH KAFLI---
    '''
    budowana na poziomie NAJGORSZEGO (najgrubszego) px wsrod uzytych kafli,
    zeby nie dodawac sztucznej szczegolowosci
    '''
    rozdzielczosci=[]
    for f in tiffs:
        with rasterio.open(f) as src:
            rozdzielczosci.append(round(src.transform[0], 2))
    return max(rozdzielczosci)


def find_min_cellsize(tiffs):
    #---NAJDROBNIEJSZY (NAJMNIEJSZY) PIKSEL WSROD PODANYCH KAFLI---
    #DWA zastosowania:
    #  1. wykrycie mieszanych rozdzielczosci wsrod uzytych kafli w
    #     generate_nfp_mosaics (porownanie z find_native_cellsize/max),
    #  2. rozdzielczosc GROUND TRUTH do liczenia bledu generalizacji
    rozdzielczosci=[]
    for f in tiffs:
        with rasterio.open(f) as src:
            rozdzielczosci.append(round(src.transform[0], 2))
    return min(rozdzielczosci)


def wyrownanie_do_siatki(bounds, cellsize):
    '''
    Wyrownuje bounds (left, bottom, right, top) do GLOBALNEJ, siatki pikseli
    floor/ceil do wielokrotnosci cellsize OD BEZWZGLEDNEGO ZERA.
    '''
    left, bottom, right, top=bounds
    left=math.floor(left / cellsize) * cellsize
    bottom=math.floor(bottom / cellsize) * cellsize
    right=math.ceil(right / cellsize) * cellsize
    top=math.ceil(top / cellsize) * cellsize
    return left, bottom, right, top


def kafle_do_pokrycia(posortowane_tiffs, geom_shape, jpt_bounds, target_cellsize):
    #---WYBIERA MINIMALNY ZESTAW KAFLI POTRZEBNY DO 100% POKRYCIA JPT---
    uzyte=[]

    for idx, f in enumerate(posortowane_tiffs):
        uzyte.append(f)

        srcs=[rasterio.open(p) for p in uzyte]
        try:
            nodata_val=srcs[0].nodata
            mos, trans=merge(srcs, bounds=jpt_bounds, res=target_cellsize,
                               resampling=Resampling.nearest,
                               target_aligned_pixels=True)
        finally:
            for s in srcs:
                s.close()

        gmask=geometry_mask([geom_shape], out_shape=mos.shape[1:],
                              transform=trans, invert=True)
        wartosci_w_jpt=mos[0][gmask]

        if nodata_val is not None:
            if np.isnan(nodata_val):
                braki=int(np.isnan(wartosci_w_jpt).sum())
            else:
                braki=int(np.sum(wartosci_w_jpt == nodata_val))
        else:
            braki=0

        print(f'[NFP] Test pokrycia: kafel {idx + 1}/{len(posortowane_tiffs)} '
              f'({Path(f).stem}) | brakujace px w JPT: {braki}')

        if braki == 0:
            print(f'[NFP] Obszar JPT w 100% pokryty ({len(posortowane_tiffs) - (idx + 1)} '
                  f'starszych kafelkow pominieto).')
            return uzyte

    print('[NFP] UWAGA: nawet po wykorzystaniu wszystkich kafelkow JPT nie jest w 100% pokryty.')
    return uzyte
