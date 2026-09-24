# -*- coding: utf-8 -*-
'''
Eksport store'u ZARR (zbudowanego przez geo_nmt.py) z powrotem do GeoTIFF - do podgladu w QGIS
albo przekazania komus, kto nie pracuje z ZARR. ZARR zostaje formatem roboczym, ten skrypt
generuje GeoTIFF na zadanie. Kazda warstwa czasowa (rok) trafia do osobnego pliku.

Kazda rozdzielczosc to osobny store .zarr (baza: <powiat>_WIELOCZASOWA.zarr, poziomy:
<powiat>_WIELOCZASOWA_5m.zarr itd.). Mozna podac sciezke dowolnego z nich albo sciezke bazy
i nazwe poziomu (grupa='5m').

UZYCIE: python zarr_do_geotiff.py store.zarr [folder_docelowy] [rok]
        python zarr_do_geotiff.py store_bazowy.zarr [folder_docelowy] [rok] [poziom, np. 5m] [zmienna]
'''

import sys
from pathlib import Path

import pandas as pd
import rioxarray  # noqa: F401
import xarray as xr

from zarr_magazyn import Magazyn


def zarr_do_geotiff(zarr_path, output_dir=None, grupa=None, rok=None, zmienna='nmt'):
    '''
    zarr_path  : sciezka do store'u .zarr
    output_dir : folder docelowy GeoTIFF (domyslnie: obok store'u)
    grupa      : (opcjonalnie) poziom rozdzielczosci, np. '5m' - wtedy zarr_path to store BAZOWY
    rok        : ktory rok wyeksportowac (None = wszystkie lata, kazdy do osobnego pliku)
    zmienna    : 'nmt' (wysokosc, domyslnie) albo 'rozdzielczosc_zrodla' (rozdzielczosc arkusza zrodlowego)
    '''
    zarr_path=Path(zarr_path)
    if grupa is not None:
        #poziom rozdzielczosci liczony z bazy: obok bazy leza stores z sufiksem (np. _5m)
        zarr_path=Magazyn(zarr_path).sciezka_grupy(grupa)
    nazwa_poziomu=zarr_path.stem

    ds=xr.open_zarr(str(zarr_path), consolidated=False, decode_coords='all')
    dane=ds[zmienna]

    output_dir=Path(output_dir) if output_dir else zarr_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    zapisane=[]
    for i, t in enumerate(dane['time'].values):
        r=int(pd.Timestamp(t).year)
        if rok is not None and r != int(rok):
            continue
        sufiks='' if zmienna == 'nmt' else f'_{zmienna}'
        sciezka=output_dir / f'{nazwa_poziomu}{sufiks}_{r}.tif'
        dane.isel(time=i).drop_vars('time').rio.to_raster(sciezka, compress='lzw')
        print(f'[ZARR->TIF] Zapisano: {sciezka}')
        zapisane.append(sciezka)

    if not zapisane:
        raise ValueError(f'brak warstwy dla roku {rok} w {zarr_path.name}')
    return zapisane


if __name__ == '__main__':
    #---UZYCIE: python zarr_do_geotiff.py store.zarr [folder_docelowy] [rok] [poziom]---
    if len(sys.argv) < 2:
        print('Uzycie: python zarr_do_geotiff.py store.zarr [folder_docelowy] [rok] [poziom, np. 5m]')
        sys.exit(1)

    zarr_do_geotiff(sys.argv[1],
                    sys.argv[2] if len(sys.argv) > 2 else None,
                    sys.argv[4] if len(sys.argv) > 4 else None,
                    int(sys.argv[3]) if len(sys.argv) > 3 else None,
                    sys.argv[5] if len(sys.argv) > 5 else 'nmt')
