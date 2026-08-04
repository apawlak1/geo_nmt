# -*- coding: utf-8 -*-
'''
generowanie mozaik NMT metodami NON-FEATURE-PRESERVING (NFP):
1. nearest neighbor
2. bilinear
3. bicubic (cubic)

->wejscie to lista "tiffs_to_mosaic" (kafle JUZ zreprojektowane do EPSG:2180,
  ale dalej w oryginalnej rozdzielczosci)
->"geometry" ta sama geometria JPT
-> "mapa_daty" to ten sam slownik nazwa_pliku+data z w folium_v3.py

!!!zbuduj_baze_natywna() buduje i zapisuje baze BLOKAMI
(2048x2048 px), zeby RAM nie zalezal od rozmiaru powiatu

!!!UDWIE ROZNE 'NATYWNE' ROZDZIELCZOSCI
find_native_cellsize() zwraca NAJGRUBSZY piksel wsrod uzytych kafli
baza budowana w tej rozdzielczosci sluzy WYLACZNIE jako wspolna,
spojna PODKLADKA pod generalizacje 'w dol' (etap 2)
nie ma sensu sztucznie upsamplowac kafli o grubszym pikselu
do rozdzielczosci najdrobniejszego kafla w zestawie tylko po to, zeby
zaraz potem i tak generalizowac wszystko w dol

!!DRUGIE zastosowanie 'bazy natywnej': jako GROUND TRUTH
do liczenia bledu generalizacji (patrz analiza.py)
do tego potrzebna jest baza w NAJDROBNIEJSZYM pikselu
inaczej referencja bylaby sama juz czesciowo zgeneralizowana (co pozniej by falszowalo)
'''

import os
import math
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
import rasterio.windows
from rasterio.merge import merge
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.transform import from_origin
from rasterio.mask import mask
from rasterio.features import geometry_mask
from shapely.geometry import shape


#---DOMYSLNE METODY NFP DO PORONANIA---
nfp_methods={'nearest': Resampling.nearest,
             'bilinear': Resampling.bilinear,
             'bicubic': Resampling.cubic,}

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


def find_trg_cellsize(tiffs, target_cellsize=None):
    if target_cellsize is not None:
        return target_cellsize

    rozdzielczosci=[]
    for f in tiffs:
        with rasterio.open(f) as src:
            rozdzielczosci.append(round(src.transform[0], 2))

    #generalizuje do najwiekszej rozdzielczosci wsrod kafli jak w processor.py
    return max(rozdzielczosci)

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


def resampling_arkusza(sciezka, docelowy_cellsize, resampling, tmp_dir):
    #---RESAMPLING JEDNEGO KAFLA (WLASNY PLIK, WLASNE GRANICE)---
    with rasterio.open(sciezka) as src:
        crs=src.crs
        nodata_val=src.nodata

        left, bottom, right, top=wyrownanie_do_siatki(src.bounds, docelowy_cellsize)
        dst_width=max(1, int(round((right - left) / docelowy_cellsize)))
        dst_height=max(1, int(round((top - bottom) / docelowy_cellsize)))
        dst_transform=from_origin(left, top, docelowy_cellsize, docelowy_cellsize)

        wypelnienie=nodata_val if nodata_val is not None else np.nan
        dane_docelowe=np.full((dst_height, dst_width), wypelnienie, dtype=src.dtypes[0])

        reproject(source=rasterio.band(src, 1), destination=dane_docelowe,
                 src_transform=src.transform, src_crs=crs,
                 dst_transform=dst_transform, dst_crs=crs,
                 src_nodata=nodata_val, dst_nodata=nodata_val,
                 resampling=resampling)

        meta=src.profile.copy()
        meta.update(height=dst_height, width=dst_width, transform=dst_transform)

    nazwa_metody=resampling.name if hasattr(resampling, 'name') else str(resampling)
    output_path=os.path.join(tmp_dir, f'{Path(sciezka).stem}_ujednolicony_{nazwa_metody}_TEMP.tif')
    with rasterio.open(output_path, 'w', **meta) as dst:
        dst.write(dane_docelowe, 1)

    return output_path


def ujednolicanie_rozdzielczosci(tiffy, native_cellsize, resampling, tmp_dir, tolerancja=0.01):
    #---SPROWADZA WSZYSTKIE KAFLE DO JEDNEJ, WSPOLNEJ ROZDZIELCZOSCI (native_cellsize)---
    #PRZED Etapem 1 (zbuduj_baze_natywna)
    #wywolywane TYLKO gdy uzyte kafle maja rozne natywne rozdzielczosci miedzy soba
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    nowa_lista=[]
    tymczasowe=[]

    for p in tiffy:
        with rasterio.open(p) as src:
            wlasna_rozdzielczosc=round(src.res[0], 2)

        if abs(wlasna_rozdzielczosc - native_cellsize) <= tolerancja:
            nowa_lista.append(p)
            continue

        print(f'[NFP] Ujednolicanie rozdzielczosci: {Path(p).name} '
              f'({wlasna_rozdzielczosc} m -> {native_cellsize} m, '
              f'metoda: {resampling.name if hasattr(resampling, "name") else resampling})')
        nowa_sciezka=resampling_arkusza(p, native_cellsize, resampling, tmp_dir)
        nowa_lista.append(nowa_sciezka)
        tymczasowe.append(nowa_sciezka)

    return nowa_lista, tymczasowe


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


def zbuduj_baze_natywna(tiffy_newest_first, jpt_bounds, native_cellsize,
                        output_path, blok_px=2048):
    #---BUDUJE JEDNA, SPOJNA BAZE (NEAREST) W PODANEJ ROZDZIELCZOSCI, BLOKAMI---
    '''
    ogolna funkcja UZYWANA Z DWOMA ROZNYMI cellsize W ZALEZNOSCI OD CELU
    +generate_nfp_mosaics() wywoluje ja z find_native_cellsize() max:
     baza jako podkladka pod generalizacje
    +zbuduj_referencje_natywna() wywoluje ja z find_min_cellsize() (min)
     baza ground truth do oceny bledu
    '''

    left, bottom, right, top=wyrownanie_do_siatki(jpt_bounds, native_cellsize)
    width=max(1, int(round((right - left) / native_cellsize)))
    height=max(1, int(round((top - bottom) / native_cellsize)))
    dst_transform=from_origin(left, top, native_cellsize, native_cellsize)

    with rasterio.open(tiffy_newest_first[0]) as pierwszy:
        crs=pierwszy.crs
        nodata_val=pierwszy.nodata
        dtype=pierwszy.dtypes[0]

    wypelnienie=nodata_val if nodata_val is not None else np.nan

    meta={'driver': 'GTiff', 'height': height, 'width': width, 'count': 1,
            'dtype': dtype, 'crs': crs, 'transform': dst_transform,
            'nodata': nodata_val, 'compress': 'lzw', 'tiled': True,
            'blockxsize': 256, 'blockysize': 256, 'BIGTIFF': 'IF_SAFER'}

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    zrodla_bounds=[]
    for p in tiffy_newest_first:
        with rasterio.open(p) as src:
            zrodla_bounds.append((p, src.bounds))

    n_bloki_x=math.ceil(width / blok_px)
    n_bloki_y=math.ceil(height / blok_px)
    print(f'[NFP] Budowa bazy ({native_cellsize} m) blokami: {n_bloki_x * n_bloki_y} '
          f'blokow ({blok_px}x{blok_px} px kazdy) -> {output_path}')

    with rasterio.open(output_path, 'w', **meta) as dst:
        for by in range(n_bloki_y):
            for bx in range(n_bloki_x):
                win=rasterio.windows.Window(
                    bx * blok_px, by * blok_px,
                    min(blok_px, width - bx * blok_px),
                    min(blok_px, height - by * blok_px))
                win_bounds=rasterio.windows.bounds(win, dst_transform)

                zrodla_w_bloku=[p for p, b in zrodla_bounds
                                  if not (b.right < win_bounds[0] or b.left > win_bounds[2]
                                          or b.top < win_bounds[1] or b.bottom > win_bounds[3])]

                if not zrodla_w_bloku:
                    pusty=np.full((1, win.height, win.width), wypelnienie, dtype=dtype)
                    dst.write(pusty, window=win)
                    del pusty
                    continue

                srcs=[rasterio.open(p) for p in zrodla_w_bloku]
                try:
                    mos_blok, _=merge(srcs, bounds=win_bounds, res=native_cellsize,
                                        resampling=Resampling.nearest,
                                        target_aligned_pixels=True)
                finally:
                    for s in srcs:
                        s.close()

                mos_blok=mos_blok[:, :win.height, :win.width]

                dst.write(mos_blok, window=win)
                del mos_blok

    return output_path


def zbuduj_referencje_natywna(uzyte_kafle, geometry, output_path, blok_px=2048):
    '''
    Buduje PRAWDZIWA referencje natywna (ground truth) do liczenia bledu
    generalizacji - metoda nearest, w NAJDROBNIEJSZYM (najmniejszym)
    pikselu wsrod podanych kafli, przycieta do granic `geometry` (JPT).

    CELOWO ODDZIELNA od bazy uzywanej wewnatrz generate_nfp_mosaics
    (ktora reprezentuje NAJGRUBSZY piksel)
    '''
    geom_shape=geometry if hasattr(geometry, 'bounds') else shape(geometry)
    jpt_bounds=geom_shape.bounds

    native_cellsize=find_min_cellsize(uzyte_kafle)
    print(f'[NFP] Budowanie referencji natywnej (nearest, {native_cellsize} m - '
          f'NAJDROBNIEJSZY piksel wsrod uzytych kafli, ground truth do oceny '
          f'bledu generalizacji) z {len(uzyte_kafle)} kafli.')

    output_dir=os.path.dirname(output_path) or '.'
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    tmp_path=os.path.join(output_dir, f'{Path(output_path).stem}_TEMP_nieciety.tif')

    zbuduj_baze_natywna(uzyte_kafle, jpt_bounds, native_cellsize, tmp_path, blok_px=blok_px)

    #---PRZYCIECIE DO DOKLADNYCH GRANIC JPT (nie tylko prostokatnych bounds)---
    with rasterio.open(tmp_path) as src:
        dane=src.read()
        trans=src.transform
        crs=src.crs
        nodata_val=src.nodata

    clip_save(dane, trans, crs, nodata_val, geom_shape, output_path)
    del dane

    try:
        os.remove(tmp_path)
    except OSError as e:
        print(f'[NFP] Nie udalo sie usunac pliku tymczasowego ({tmp_path}): {e}')

    print(f'[NFP] Referencja natywna gotowa: {output_path}')
    return output_path, native_cellsize


def _resampluj_baze(native_base_path, target_cellsize, resampling):
    #---RESAMPLING JUZ SPOJNEJ BAZY DO target_cellsize---
    with rasterio.open(native_base_path) as src:
        crs=src.crs
        nodata_val=src.nodata
        left, bottom, right, top=src.bounds

        dst_width=max(1, int(round((right - left) / target_cellsize)))
        dst_height=max(1, int(round((top - bottom) / target_cellsize)))
        dst_transform=from_origin(left, top, target_cellsize, target_cellsize)

        wypelnienie=nodata_val if nodata_val is not None else np.nan
        dst=np.full((1, dst_height, dst_width), wypelnienie, dtype=src.dtypes[0])

        reproject(source=rasterio.band(src, 1), destination=dst,
                 src_transform=src.transform, src_crs=crs,
                 dst_transform=dst_transform, dst_crs=crs,
                 src_nodata=nodata_val, dst_nodata=nodata_val,
                 resampling=resampling)

    return dst, dst_transform, crs, nodata_val


def clip_save(mos, trans, crs, nodata_val, geom_shape, output_path):
    #---ZAOKRAGLENIE WYSOKOSCI DO 2 MSC PO PRZECINKU---
    mos=np.round(mos, 2)

    meta={
        'driver': 'GTiff',
        'height': mos.shape[1],
        'width': mos.shape[2],
        'count': 1,
        'dtype': mos.dtype,
        'crs': crs,
        'transform': trans,
        'nodata': nodata_val,
        'compress': 'lzw',
    }

    with rasterio.MemoryFile() as memfile:
        with memfile.open(**meta) as tmp_ds:
            tmp_ds.write(mos)
        with memfile.open() as tmp_ds:
            out_image, out_transform=mask(tmp_ds, [geom_shape], crop=True)

    out_image=np.round(out_image, 2)
    meta.update({
        'height': out_image.shape[1],
        'width': out_image.shape[2],
        'transform': out_transform,
    })

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, 'w', **meta) as dst:
        dst.write(out_image)

    return output_path


def generate_nfp_mosaics(tiffs_to_mosaic, geometry, mapa_daty, output_dir, base_name,
                         target_cellsize=None, metody=None, uzyte_kafle=None,
                         return_uzyte_kafle=False, baza_natywna=None,
                         return_baza_natywna=False, blok_px=2048,
                         metoda_ujednolicania=Resampling.bilinear):

    #---GLOWNA FUNKCJA MODULU---
    if not tiffs_to_mosaic:
        print('[NFP] Brak plikow wejsciowych, pominieto.')
        pusty={}
        if return_uzyte_kafle and return_baza_natywna:
            return pusty, [], None
        if return_uzyte_kafle:
            return pusty, []
        if return_baza_natywna:
            return pusty, None
        return pusty

    if metody is None:
        metody=nfp_methods

    target_cellsize=find_trg_cellsize(tiffs_to_mosaic, target_cellsize)
    print(f'[NFP] Docelowa rozdzielczosc mozaiki: {target_cellsize} m')

    geom_shape=shape(geometry)
    jpt_bounds=geom_shape.bounds  # (left, bottom, right, top)

    if uzyte_kafle is None:
        posortowane=sorted(tiffs_to_mosaic,
                             key=lambda p: find_date(p, mapa_daty),
                             reverse=True)
        uzyte_kafle=kafle_do_pokrycia(posortowane, geom_shape, jpt_bounds, target_cellsize)
    else:
        print(f'[NFP] Pominieto test pokrycia. Uzyto wczesniej wyznaczonego '
              f'zestawu {len(uzyte_kafle)} kafli.')

    #---ETAP 1: SPOJNA BAZA (NEAREST, NAJGRUBSZY PIKSEL WSROD UZYTYCH KAFLI)---
    #podkladka pod generalizacje "w dol" (Etap 2 ponizej), NIE ground truth.
    baza_zbudowana_tutaj=baza_natywna is None
    tymczasowe_ujednolicone=[]
    if baza_zbudowana_tutaj:
        native_cellsize=find_native_cellsize(uzyte_kafle)
        najdrobniejszy_wsrod_uzytych=find_min_cellsize(uzyte_kafle)

        #---MIESZANE ROZDZIELCZOSCI WSROD UZYTYCH KAFLI: UJEDNOLICENIE PRZED MERGE---
        '''
        native_cellsize to teraz NAJGRUBSZY piksel wsrod uzytych
        kazdy #kafel DROBNIEJSZY od niego jestresamplowany W GORE
        do wspolnej, najgrubszej rozdzielczosci
        dopiero WTEDY nizej nastepuje laczenie metoda nearest
        '''
        kafle_do_bazy=uzyte_kafle
        if abs(najdrobniejszy_wsrod_uzytych - native_cellsize) > 0.01:
            print(f'[NFP] Wykryto mieszane rozdzielczosci wsrod uzytych kafli '
                  f'({najdrobniejszy_wsrod_uzytych} m - {native_cellsize} m) - '
                  f'ujednolicanie do {native_cellsize} m metoda '
                  f'{metoda_ujednolicania.name if hasattr(metoda_ujednolicania, "name") else metoda_ujednolicania} '
                  f'przed budowa bazy.')
            tmp_dir_ujednolicenia=os.path.join(output_dir, f'{base_name}_ujednolicanie_TEMP')
            kafle_do_bazy, tymczasowe_ujednolicone=ujednolicanie_rozdzielczosci(
                uzyte_kafle, native_cellsize, metoda_ujednolicania, tmp_dir_ujednolicenia)

        print(f'[NFP] Budowanie spojnej bazy (nearest, {native_cellsize} m) '
              f'z {len(kafle_do_bazy)} kafli.')
        native_base_path=os.path.join(output_dir, f'{base_name}_baza_nfp_TEMP.tif')
        #---WAZNE: wywolanie zbuduj_baze_natywna(), NIE baza_natywna() ---
        #(baza_natywna to parametr tej funkcji, patrz UWAGA na gorze modulu)
        baza_natywna=zbuduj_baze_natywna(
            kafle_do_bazy, jpt_bounds, native_cellsize, native_base_path, blok_px=blok_px)

        for p in tymczasowe_ujednolicone:
            try:
                os.remove(p)
            except OSError as e:
                print(f'[NFP] Nie udalo sie usunac pliku tymczasowego ({p}): {e}')
        if tymczasowe_ujednolicone:
            try:
                os.rmdir(os.path.dirname(tymczasowe_ujednolicone[0]))
            except OSError:
                pass
    else:
        print(f'[NFP] Pominieto budowe bazy. Uyzto wczesniej wyznaczonej: {baza_natywna}')

    #---ETAP 2: RESAMPLING BAZY DO target_cellsize, PO JEDNYM PRZEJSCIU NA METODE---
    wyniki={}
    for nazwa, resampling in metody.items():
        output_path=os.path.join(output_dir, f'{base_name}_NFP_{nazwa}.tif')
        print(f'[NFP] Resampling bazy metoda: {nazwa}')
        mos_target, trans_target, crs, nodata_val=_resampluj_baze(
            baza_natywna, target_cellsize, resampling)
        clip_save(mos_target, trans_target, crs, nodata_val, geom_shape, output_path)
        wyniki[nazwa]=output_path
        print(f'[NFP] Zapisano: {output_path}')

    if baza_zbudowana_tutaj and not return_baza_natywna:
        try:
            os.remove(baza_natywna)
            print(f'[NFP] Usunieto tymczasowa baze: {baza_natywna}')
        except OSError as e:
            print(f'[NFP] Nie udalo sie usunac tymczasowej bazy ({baza_natywna}): {e}')

    baza_do_zwrotu=baza_natywna
    if return_uzyte_kafle and return_baza_natywna:
        return wyniki, uzyte_kafle, baza_do_zwrotu
    if return_uzyte_kafle:
        return wyniki, uzyte_kafle
    if return_baza_natywna:
        return wyniki, baza_do_zwrotu
    return wyniki