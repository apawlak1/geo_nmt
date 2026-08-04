# -*- coding: utf-8 -*-


import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.enums import Resampling
from rasterio.mask import mask
from rasterio.io import MemoryFile
from shapely.geometry import shape

from nfp_mosaics import find_date, kafle_do_pokrycia


def zbuduj_mape_pochodzenia(tiffs_to_mosaic, geometry, mapa_daty, target_cellsize, output_path,
                            uzyte_kafle=None):
    #kazdy piksel dostaje id kafla ktory go dostarczyl
    #do przestawienia za pomoca kolorowej ilustracji jakich kafli uzyto, jaki jest ich zakres
    #ID=0 oznacza brak pokrycia
    #wystepuje tylko wtedy, gdy obszar nie jest w 100% pokryty

    if not tiffs_to_mosaic:
        print('[MAPA_KAFLI] Brak plikow wejsciowych, pominieto.')
        return None, {}

    geom_shape=shape(geometry)
    jpt_bounds=geom_shape.bounds

    #---uzyte_kafle: PONOWNE UZYCIE ZAMIAST WLASNEGO TESTU POKRYCIA---
    #wykorzystuje test pokrycia wykonany przy budowie mozaiki: zawsze spojna!!
    
    if uzyte_kafle is None:
        posortowane=sorted(tiffs_to_mosaic, key=lambda p: find_date(p, mapa_daty), reverse=True)
        uzyte_kafle=kafle_do_pokrycia(posortowane, geom_shape, jpt_bounds, target_cellsize)
    else:
        print(f'[MAPA_KAFLI] Uzywam wczesniej wyznaczonego zestawu kafli '
              f'({len(uzyte_kafle)}).')

    #---UNIKALNE ID DLA KAZDEGO KAFLA (1, 2, 3...), 0=BRAK DANYCH---
    mapowanie_id_nazwa={}
    memfiles=[]
    zrodla=[]
    crs_wyjsciowy=None

    try:
        for idx, sciezka in enumerate(uzyte_kafle, start=1):
            with rasterio.open(sciezka) as src:
                dane=src.read(1)
                nodata_val=src.nodata
                profil=src.profile.copy()
                if crs_wyjsciowy is None:
                    crs_wyjsciowy=src.crs

            if nodata_val is not None:
                if np.isnan(nodata_val):
                    maska_danych=~np.isnan(dane)
                else:
                    maska_danych=dane != nodata_val
            else:
                maska_danych=np.ones(dane.shape, dtype=bool)

            #---RASTER Z SAMYMI ID ZAMIAST WYSOKOSCI---
            id_raster=np.zeros(dane.shape, dtype='int32')
            id_raster[maska_danych]=idx

            profil.update(dtype='int32', nodata=0, count=1)

            memfile=MemoryFile()
            with memfile.open(**profil) as tmp:
                tmp.write(id_raster, 1)
            memfiles.append(memfile)
            zrodla.append(memfile.open())

            mapowanie_id_nazwa[idx]=Path(sciezka).name

        print(f'[MAPA_KAFLI] Przypisano ID dla {len(mapowanie_id_nazwa)} kafli.')

        #---LACZENIE NEAREST---
        mos, trans=merge(zrodla, bounds=jpt_bounds, res=target_cellsize,
                           resampling=Resampling.nearest, target_aligned_pixels=True,
                           nodata=0)
    finally:
        for ds in zrodla:
            ds.close()
        for mf in memfiles:
            mf.close()

    meta={'driver': 'GTiff',
            'height': mos.shape[1], 'width': mos.shape[2],
            'count': 1, 'dtype': 'int32',
            'crs': crs_wyjsciowy, 'transform': trans,
            'nodata': 0, 'compress': 'lzw',}

    #---PRZYCIECIE DO GRANIC JPT (w pamieci, bez pliku tymczasowego)---
    with MemoryFile() as memfile:
        with memfile.open(**meta) as tmp_ds:
            tmp_ds.write(mos)
        with memfile.open() as tmp_ds:
            out_image, out_transform=mask(tmp_ds, [geom_shape], crop=True)

    meta.update({'height': out_image.shape[1], 'width': out_image.shape[2],
                 'transform': out_transform,})

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, 'w', **meta) as dst:
        dst.write(out_image)
        #---METADANE WBUDOWANE W PLIK: JSON Z MAPOWANIEM ID -> NAZWA PLIKU---
        dst.update_tags(mapowanie_id_nazwa=json.dumps(mapowanie_id_nazwa, ensure_ascii=False))

    #---SIDECAR .json OBOK RASTRA (wygodniejszy do wczytania bez otwierania TIFF)---
    sidecar_path=str(Path(output_path).with_suffix('.json'))
    with open(sidecar_path, 'w', encoding='utf-8') as f:
        json.dump(mapowanie_id_nazwa, f, ensure_ascii=False, indent=2)

    print(f'[MAPA_KAFLI] Zapisano mape pochodzenia: {output_path}')
    print(f'[MAPA_KAFLI] Mapowanie ID->nazwa zapisano w: {sidecar_path}')

    return output_path, mapowanie_id_nazwa