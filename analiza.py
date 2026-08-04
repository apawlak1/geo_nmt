# -*- coding: utf-8 -*-
'''
OKRESLENIE METODY GENERALIZACJI O NAJMNIEJSZYM BLEDZIE
'''

import os
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from nfp_mosaics import generate_nfp_mosaics, nfp_methods, zbuduj_referencje_natywna
from fp_filters import fp_methods, sygnatura_skalowa_cva, wczytaj_nmt, zapisz_nmt


def _fmt(wartosc, jednostka=' m', miejsca=3):
    #---BEZPIECZNE FORMATOWANIE LICZBY (zwraca '-' zamiast rzucac TypeError na None)---
    if wartosc is None:
        return '-'
    return f'{wartosc:.{miejsca}f}{jednostka}'


def zakres_rozdzielczosci(tiffs_to_mosaic):
    #---ZWRACA NAJMNIEJSZY/NAJWIEKSZY PX WSROD KAFLI WEJSCIOWYCH---
    rozdzielczosci=[]
    for f in tiffs_to_mosaic:
        with rasterio.open(f) as src:
            rozdzielczosci.append(round(src.transform[0], 2))
    return min(rozdzielczosci), max(rozdzielczosci)


def raster_roznicowy(path_referencja, path_porownanie, output_path):
    #LICZY REFERENCJA-POROWNANIE, 'POROWNANIE' SPROWADZONE NA SIATKE 'REFERENCJI'
    with rasterio.open(path_referencja) as ref:
        ref_data=ref.read(1)
        ref_nodata=ref.nodata
        ref_transform=ref.transform
        ref_crs=ref.crs
        ref_shape=(ref.height, ref.width)

        with rasterio.open(path_porownanie) as por:
            with WarpedVRT(por, crs=ref_crs, transform=ref_transform,
                           width=ref_shape[1], height=ref_shape[0],
                           resampling=Resampling.nearest) as vrt:
                por_data=vrt.read(1)
                por_nodata=vrt.nodata

    maska_valid=np.ones(ref_data.shape, dtype=bool)
    if ref_nodata is not None:
        if np.isnan(ref_nodata):
            maska_valid &= ~np.isnan(ref_data)
        else:
            maska_valid &= (ref_data != ref_nodata)
    if por_nodata is not None:
        if np.isnan(por_nodata):
            maska_valid &= ~np.isnan(por_data)
        else:
            maska_valid &= (por_data != por_nodata)

    roznica=np.full(ref_data.shape, np.nan, dtype='float32')
    roznica[maska_valid]=ref_data[maska_valid].astype('float32') - por_data[maska_valid].astype('float32')

    meta={'driver': 'GTiff',
          'height': ref_shape[0], 'width': ref_shape[1],
          'count': 1, 'dtype': 'float32',
          'crs': ref_crs, 'transform': ref_transform,
          'nodata': np.nan, 'compress': 'lzw',}
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, 'w', **meta) as dst:
        dst.write(roznica, 1)

    wartosci=roznica[maska_valid]
    stats={'n_px': int(wartosci.size),
           'MAE': float(np.mean(np.abs(wartosci))) if wartosci.size else None,
           'RMSE': float(np.sqrt(np.mean(wartosci ** 2))) if wartosci.size else None,
           'min': float(np.min(wartosci)) if wartosci.size else None,
           'max': float(np.max(wartosci)) if wartosci.size else None,
           'std': float(np.std(wartosci)) if wartosci.size else None,}

    if stats['n_px'] == 0:
        print(f'[STATYSTYKI RASTRA ROZNICOWEGO]'
              f'{os.path.basename(output_path)}: '
              f'BRAK WSPOLNYCH PIKSELI Z DANYMI.'
              f'Referencja i porownanie sie NIE POKRYWAJA')
    else:
        print(f'[STATYSTYKI RASTRA ROZNICOWEGO]'
              f'{os.path.basename(output_path)} '
              f'n={stats["n_px"]} | '
              f'MAE={_fmt(stats["MAE"])} | RMSE={_fmt(stats["RMSE"])} | '
              f'min={_fmt(stats["min"])} | max={_fmt(stats["max"])} | '
              f'std={_fmt(stats["std"])}')
    return stats


def raport_txt(wyniki, output_dir, base_name):
    #---RAPORT TEKSTOWY ZE STATYSTYKAMI BLEDU GENERALIZACJI---
    roznice=wyniki.get('roznice', {})
    if not roznice:
        print('[ANALIZA] Brak statystyk do zapisania w raporcie.'\
              f'NIE UTWORZONO RAPORTU.')
        return None

    linie=[]
    linie.append(f'RAPORT ANALIZY GENERALIZACJI NMT: {base_name}')
    linie.append('-'*60)
    linie.append(f"Rozdzielczosc natywna (referencja):  {wyniki.get('target_cellsize_natywna')} m")
    linie.append(f"Rozdzielczosc eksperymentalna:       {wyniki.get('target_cellsize_eksperymentalna')} m")
    linie.append('')

    for nazwa, stats in roznice.items():
        linie.append(f'---METODA {nazwa.upper()}---')
        linie.append(f"liczba pikseli (n_px): {stats.get('n_px')}")
        linie.append(f"MAE ={_fmt(stats.get('MAE'))}")
        linie.append(f"RMSE={_fmt(stats.get('RMSE'))}")
        linie.append(f"min ={_fmt(stats.get('min'))}")
        linie.append(f"max ={_fmt(stats.get('max'))}")
        linie.append(f"std ={_fmt(stats.get('std'))}")
        if stats.get('n_px') == 0:
            linie.append('  UWAGA: brak wspolnych pikseli z danymi miedzy referencja a ta mozaika.')
        linie.append('')

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    report_path=os.path.join(output_dir, f'{base_name}_RAPORT.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(linie))

    print(f'[ANALIZA] Raport zapisano w: {report_path}')
    return report_path


def analiza_wielorozdzielczosciowa(tiffs_to_mosaic, geometry, mapa_daty, output_dir, base_name,
                                   experimental_res=20.0, metody=None,
                                   uzyte_kafle=None, baza_natywna=None,
                                   referencja_natywna=None):
    '''
    zwraca dict:
    {'natywna': {'nearest': sciezka},
     'eksperymentalna': {'nearest': sciezka, 'bilinear': sciezka, 'bicubic': sciezka},
     'roznice': {'nearest': stats, 'bilinear': stats, 'bicubic': stats},
     'target_cellsize_natywna': float,
     'target_cellsize_eksperymentalna': float,}

    uzyte_kafle : opcjonalne - gotowy wynik testu pokrycia JPT, do reuzycia.
    '''
    if metody is None:
        metody=nfp_methods

    if experimental_res is None:
        experimental_res=20.0

    if not tiffs_to_mosaic:
        print('[ANALIZA] Brak plikow wejsciowych, pominieto.')
        return {}

    najmniejszy, najwiekszy=zakres_rozdzielczosci(tiffs_to_mosaic)
    print(f'[ANALIZA] Zakres rozdzielczosci wsrod kafli: {najmniejszy} m - {najwiekszy} m')

    #---KROK 1: PRAWDZIWA REFERENCJA NATYWNA---
    #ground truth, najdrobniejszy piksel wsrod uzytych kafli
    # NIEZALEZNA od 'baza_natywna'
    print(f'\n[ANALIZA] ---Krok 1: referencja natywna (ground truth)---')

    if referencja_natywna is not None and os.path.exists(referencja_natywna):
        print(f'[ANALIZA] Uzyto wczesniej zbudowanej referencji natywnej: {referencja_natywna}')
        referencja=referencja_natywna
    else:
        if uzyte_kafle is None:
            #---BRAK GOTOWEJ LISTY KAFLI: WYZNACZAM JA (test pokrycia JPT)---
            #target_cellsize tutaj nie ma znaczenia (metody={} - nie liczy zadnej mozaiki)
            #interesuje mnie TYLKO lista uzyte_kafle
            _, uzyte_kafle=generate_nfp_mosaics(
                tiffs_to_mosaic=tiffs_to_mosaic, geometry=geometry, mapa_daty=mapa_daty,
                output_dir=output_dir, base_name=f'{base_name}_pomocniczo',
                target_cellsize=najmniejszy, metody={}, return_uzyte_kafle=True)

        referencja_path=os.path.join(output_dir, f'{base_name}_natywna_NFP_nearest.tif')
        referencja, native_cellsize_referencji=zbuduj_referencje_natywna(
            uzyte_kafle, geometry, referencja_path)
        najmniejszy=native_cellsize_referencji

    mozaiki_natywne={'nearest': referencja}
    print(f'[ANALIZA] Referencja do rastra roznicowego: {referencja}')

    #--- KROK 2: MOZAIKI W ROZDZIELCZOSCI EKSPERYMENTALNEJ---
    #uzywa 'baza_natywna'  w NAJGRUBSZYM pikselu z uzytych kafli
    #NIE ma to zwiazku z referencja z Kroku 1 powyzej
    print(f'\n[ANALIZA] ---Krok 2: mozaiki w rozdzielczosci eksperymentalnej {experimental_res} m)---')
    mozaiki_eksperymentalne=generate_nfp_mosaics(
        tiffs_to_mosaic=tiffs_to_mosaic,
        geometry=geometry, mapa_daty=mapa_daty,
        output_dir=output_dir, base_name=f'{base_name}_exp{int(experimental_res)}m',
        target_cellsize=experimental_res, metody=metody,
        uzyte_kafle=uzyte_kafle, baza_natywna=baza_natywna,)

    #---3: RASTRY ROZNICOWE (eksperymentalna minus referencja)---
    roznice={}
    if referencja is not None:
        print('\n[ANALIZA] ---Krok 3: rastry roznicowe (eksperymentalna minus natywna)---')
        for nazwa, sciezka in mozaiki_eksperymentalne.items():
            output_diff=os.path.join(
                output_dir, f'{base_name}_ROZNICA_{nazwa}_exp{int(experimental_res)}m.tif')
            try:
                stats=raster_roznicowy(referencja, sciezka, output_diff)
                roznice[nazwa]=stats
            except Exception as e:
                print(f'[ANALIZA] BLAD przy liczeniu roznicy dla metody {nazwa}: {e}')

    wynik={'natywna': mozaiki_natywne,
           'eksperymentalna': mozaiki_eksperymentalne,
           'roznice': roznice,
           'target_cellsize_natywna': najmniejszy,
           'target_cellsize_eksperymentalna': experimental_res,}

    #---BEZ AUTOMATYCZNEGO RAPORTU TUTAJ (dubluje sie z zapisz_raport_nfp w wersja_dev.py)---

    return wynik

#---ANALIZA METOD FEATURE-PRESERVING (FP): Perona-Malik/Bilateral/FPDEMS---
def _stats_roznicy_array(dem_oryginal: np.ndarray, dem_po_fp: np.ndarray) -> dict:
    maska_valid=~np.isnan(dem_oryginal) & ~np.isnan(dem_po_fp)
    wartosci=dem_oryginal[maska_valid].astype('float64') - dem_po_fp[maska_valid].astype('float64')

    if wartosci.size==0:
        return {'n_px': 0, 'MAE': None, 'RMSE': None, 'min': None, 'max': None, 'std': None}

    return {'n_px': int(wartosci.size),
            'MAE': float(np.mean(np.abs(wartosci))),
            'RMSE': float(np.sqrt(np.mean(wartosci ** 2))),
            'min': float(np.min(wartosci)),
            'max': float(np.max(wartosci)),
            'std': float(np.std(wartosci)),}


def raport_fp_txt(wyniki_fp, output_dir, base_name):
    roznice=wyniki_fp.get('roznice_fp', {})
    if not roznice:
        print('[ANALIZA] Brak statystyk do zapisania w raporcie.'\
                      f'NIE UTWORZONO RAPORTU.')
        return None

    linie=[]
    linie.append(f'RAPORT ANALIZY FEATURE-PRESERVING: {base_name}')
    linie.append('-'*60)
    linie.append('')

    for nazwa, stats in roznice.items():
        linie.append(f'---METODA {nazwa.upper()}---')
        linie.append(f"liczba pikseli (n_px): {stats.get('n_px')}")
        linie.append(f"MAE ={_fmt(stats.get('MAE'))}")
        linie.append(f"RMSE={_fmt(stats.get('RMSE'))}")
        linie.append(f"min ={_fmt(stats.get('min'))}")
        linie.append(f"max ={_fmt(stats.get('max'))}")
        linie.append(f"std ={_fmt(stats.get('std'))}")
        if stats.get('n_px') == 0:
            linie.append('  UWAGA: brak wspolnych pikseli z danymi miedzy oryginalem a ta metoda FP.')
        linie.append('')

    linie.append('---SYGNATURA SKALOWA CVA---')
    cva_oryginal=wyniki_fp.get('cva_oryginal', {})
    linie.append(f"ORYGINAL: {cva_oryginal}")
    for nazwa, cva in wyniki_fp.get('cva_fp', {}).items():
        linie.append(f"{nazwa.upper()}: {cva}")
    linie.append('')

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    report_path=os.path.join(output_dir, f'{base_name}_RAPORT_FP.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(linie))

    print(f'[ANALIZA FP] Raport zapisano w: {report_path}')
    return report_path


def analiza_fp_generalizacji(dem_path, output_dir, base_name, metody_fp=None,
                             rozmiary_okien_cva=(3, 5, 7, 9, 11, 15, 21)):
    if metody_fp is None:
        metody_fp=fp_methods

    dem_oryginal, cellsize, profil=wczytaj_nmt(dem_path)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    wyniki_fp={}
    roznice_fp={}
    rastry_roznicowe_fp={}
    cva_fp={}

    print(f'[ANALIZA FP] Sygnatura CVA oryginalu (rozdzielczosc {cellsize} m)...')
    cva_oryginal=sygnatura_skalowa_cva(dem_oryginal, cellsize, rozmiary_okien_cva)

    for nazwa, funkcja in metody_fp.items():
        print(f'[ANALIZA FP] Stosowanie metody: {nazwa}')
        dem_po_fp=funkcja(dem_oryginal, cellsize)

        output_path=os.path.join(output_dir, f'{base_name}_FP_{nazwa}.tif')
        zapisz_nmt(dem_po_fp, profil, output_path)
        wyniki_fp[nazwa]=output_path

        stats=_stats_roznicy_array(dem_oryginal, dem_po_fp)
        roznice_fp[nazwa]=stats
        print(f'[ANALIZA FP] {nazwa}: MAE={_fmt(stats["MAE"])} | RMSE={_fmt(stats["RMSE"])}')

        maska_valid_fp=~np.isnan(dem_oryginal) & ~np.isnan(dem_po_fp)
        roznica_raster=np.full(dem_oryginal.shape, np.nan, dtype='float32')
        roznica_raster[maska_valid_fp]=(
            dem_oryginal[maska_valid_fp] - dem_po_fp[maska_valid_fp]).astype('float32')

        roznica_path=os.path.join(output_dir, f'{base_name}_ROZNICA_FP_{nazwa}.tif')
        zapisz_nmt(roznica_raster, profil, roznica_path)
        rastry_roznicowe_fp[nazwa]=roznica_path
        del roznica_raster
        print(f'[ANALIZA FP] Raster roznicowy zapisany w: {roznica_path}')

        cva_fp[nazwa]=sygnatura_skalowa_cva(dem_po_fp, cellsize, rozmiary_okien_cva)

    wynik={'wyniki_fp': wyniki_fp,
             'roznice_fp': roznice_fp,
             'rastry_roznicowe_fp': rastry_roznicowe_fp,
             'cva_oryginal': cva_oryginal,
             'cva_fp': cva_fp,}

    #---BEZ AUTOMATYCZNEGO RAPORTU TUTAJ (dubluje sie z zapisz_raport_fp w wersja_dev.py)---
    return wynik