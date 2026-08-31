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
from fp_filters import fp_methods, sygnatura_skalowa_cva, oblicz_cva, wczytaj_nmt, zapisz_nmt


def _fmt(wartosc, jednostka=' m', miejsca=3):
    #---BEZPIECZNE FORMATOWANIE LICZBY (zwraca '-' zamiast rzucac TypeError na None)---
    if wartosc is None:
        return '-'
    return f'{wartosc:.{miejsca}f}{jednostka}'


def _round_cva_dict(cva_dict, miejsca=3):
    #---ZAOKRAGLA WARTOSCI CVA (KLUCZ=ROZMIAR OKNA, WARTOSC=CVA) DO 'miejsca' MIEJSC---
    return {okno: (round(wartosc, miejsca) if wartosc is not None else None)
            for okno, wartosc in cva_dict.items()}


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

    #---BLAD WZGLEDEM DANYCH SPRZED FILTRACJI (wielkosc zmiany wprowadzonej przez filtr)---
    linie.append('=== BLAD WZGLEDEM DANYCH SPRZED FILTRACJI (input vs output filtra) ===')
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

    #---BLAD WZGLEDEM REFERENCJI NATYWNEJ (ground truth) - WERYFIKACJA TEZY PRACY---
    #czy (resampling + filtracja) przyblizza wynik do prawdy bardziej niz sam resampling?
    #TO JEST KLUCZOWE: bez tego bloku praca nigdy nie sprawdza wlasnej hipotezy badawczej
    roznice_vs_ref=wyniki_fp.get('roznice_vs_referencja', {})
    if roznice_vs_ref:
        linie.append('=== BLAD WZGLEDEM REFERENCJI NATYWNEJ (ground truth) ===')
        linie.append('(czy filtracja przyblizyla wynik do prawdziwej rzezby terenu,')
        linie.append(' a nie tylko zmienila dane wzgledem samych siebie)')
        linie.append('')

        stats_wejscie=roznice_vs_ref.get('przed_filtracja')
        if stats_wejscie is not None:
            linie.append('---PRZED FILTRACJA (sam resampling, punkt odniesienia)---')
            linie.append(f"liczba pikseli (n_px): {stats_wejscie.get('n_px')}")
            linie.append(f"MAE ={_fmt(stats_wejscie.get('MAE'))}")
            linie.append(f"RMSE={_fmt(stats_wejscie.get('RMSE'))}")
            linie.append('')

        for nazwa, stats in roznice_vs_ref.items():
            if nazwa == 'przed_filtracja':
                continue
            linie.append(f'---METODA {nazwa.upper()} (po filtracji)---')
            linie.append(f"liczba pikseli (n_px): {stats.get('n_px')}")
            linie.append(f"MAE ={_fmt(stats.get('MAE'))}")
            linie.append(f"RMSE={_fmt(stats.get('RMSE'))}")

            if stats_wejscie is not None and stats_wejscie.get('RMSE') is not None and stats.get('RMSE') is not None:
                delta_rmse=stats['RMSE'] - stats_wejscie['RMSE']
                kierunek='POPRAWA' if delta_rmse < 0 else ('POGORSZENIE' if delta_rmse > 0 else 'BEZ ZMIAN')
                linie.append(f"delta RMSE vs przed filtracja: {delta_rmse:+.4f} m ({kierunek})")
            linie.append('')
    else:
        linie.append('=== BLAD WZGLEDEM REFERENCJI NATYWNEJ: NIE OBLICZONO ===')
        linie.append('(nie podano referencji natywnej przy wywolaniu analiza_fp_generalizacji)')
        linie.append('')

    linie.append('---SYGNATURA SKALOWA CVA (zaokraglone do 3 msc po przecinku)---')
    cva_oryginal=_round_cva_dict(wyniki_fp.get('cva_oryginal', {}))
    linie.append(f"ORYGINAL: {cva_oryginal}")
    for nazwa, cva in wyniki_fp.get('cva_fp', {}).items():
        linie.append(f"{nazwa.upper()}: {_round_cva_dict(cva)}")
    linie.append('')

    #---MAPY RASTROWE CVA (przestrzenny rozklad szorstkosci terenu)---
    cva_rastry=wyniki_fp.get('cva_rastry', {})
    if cva_rastry:
        linie.append('---MAPY RASTROWE CVA (zapisane pliki .tif)---')
        for nazwa, sciezka in cva_rastry.items():
            linie.append(f"{nazwa.upper()}: {sciezka}")
        linie.append('')

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    report_path=os.path.join(output_dir, f'{base_name}_RAPORT_FP.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(linie))

    print(f'[ANALIZA FP] Raport zapisano w: {report_path}')
    return report_path


def analiza_fp_generalizacji(dem_path, output_dir, base_name, metody_fp=None,
                             rozmiary_okien_cva=(3, 5, 7, 9, 11, 15, 21),
                             referencja_natywna=None, cva_raster_okno=9):
    '''
    referencja_natywna : sciezka do rastra ground truth (najdrobniejszy piksel
        wsrod arkuszy zrodlowych, patrz zbuduj_referencje_natywna w nfp_mosaics.py).

        !!!KLUCZOWE!!!: bez podania tego argumentu funkcja liczy WYLACZNIE blad
        filtracji wzgledem samej siebie (ile filtr zmienil dane), a NIE sprawdza
        czy filtracja faktycznie przyblizyla wynik do prawdziwej rzezby terenu.
        Teza pracy ("filtracja feature-preserving po resamplingu daje model o
        WYZSZEJ WIARYGODNOSCI GEOMETRYCZNEJ niz sam resampling") wymaga
        porownania WZGLEDEM REFERENCJI, nie tylko przed/po filtracji miedzy soba.
        Podaj referencja_natywna zawsze, gdy jest dostepna (patrz
        analiza_wielorozdzielczosciowa - ten sam plik, ktory tam sluzy jako
        ground truth do oceny bledu resamplingu).

    cva_raster_okno : rozmiar okna (w pikselach), dla ktorego zapisywana jest
        PRZESTRZENNA MAPA CVA (raster .tif, wartosci 0-1 na kazdym pikselu),
        w odroznieniu od sygnatury_skalowa_cva, ktora zwraca tylko SREDNIA
        wartosc CVA (jedna liczba) na cale zobrazowanie, dla kilku rozmiarow
        okna naraz. Mapa rastrowa pozwala zobaczyc GDZIE dana metoda splaszcza
        teren, a nie tylko O ILE (w agregacie). Domyslnie generowana jest
        TYLKO dla jednego rozmiaru okna (zeby nie mnozyc plikow x7 rozmiarow),
        wybierz wartosc reprezentatywna dla analizy (np. srodek zakresu
        rozmiary_okien_cva).
    '''
    if metody_fp is None:
        metody_fp=fp_methods

    dem_oryginal, cellsize, profil=wczytaj_nmt(dem_path)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    wyniki_fp={}
    roznice_fp={}
    rastry_roznicowe_fp={}
    cva_fp={}
    cva_rastry={}
    roznice_vs_referencja={}
    rastry_roznicowe_vs_referencja={}

    print(f'[ANALIZA FP] Sygnatura CVA oryginalu (rozdzielczosc {cellsize} m)...')
    cva_oryginal=sygnatura_skalowa_cva(dem_oryginal, cellsize, rozmiary_okien_cva)

    #---MAPA RASTROWA CVA DLA ORYGINALU (przed jakakolwiek filtracja)---
    print(f'[ANALIZA FP] Zapis mapy rastrowej CVA oryginalu (okno {cva_raster_okno} px)...')
    cva_mapa_oryginal=oblicz_cva(dem_oryginal, cellsize, rozmiar_okna=cva_raster_okno)
    cva_mapa_oryginal_path=os.path.join(
        output_dir, f'{base_name}_CVA_okno{cva_raster_okno}_oryginal.tif')
    zapisz_nmt(cva_mapa_oryginal, profil, cva_mapa_oryginal_path)
    cva_rastry['oryginal']=cva_mapa_oryginal_path
    del cva_mapa_oryginal

    #---BLAD WZGLEDEM REFERENCJI NATYWNEJ, DLA DANYCH SPRZED FILTRACJI---
    #to jest PUNKT ODNIESIENIA: samo MAE/RMSE resamplingu wzgledem ground truth,
    #z ktorym porownamy pozniej kazda z trzech metod filtracji
    if referencja_natywna is not None:
        print(f'[ANALIZA FP] Blad WEJSCIA (przed filtracja) wzgledem referencji natywnej...')
        diff_wejscie_path=os.path.join(
            output_dir, f'{base_name}_ROZNICA_vs_REF_przed_filtracja.tif')
        stats_wejscie=raster_roznicowy(referencja_natywna, dem_path, diff_wejscie_path)
        roznice_vs_referencja['przed_filtracja']=stats_wejscie
        rastry_roznicowe_vs_referencja['przed_filtracja']=diff_wejscie_path
        print(f'[ANALIZA FP] przed_filtracja vs REF: '
              f'MAE={_fmt(stats_wejscie["MAE"])} | RMSE={_fmt(stats_wejscie["RMSE"])}')
    else:
        print('[ANALIZA FP] UWAGA: referencja_natywna nie podana - blad filtracji '
              'zostanie policzony WYLACZNIE wzgledem danych sprzed filtracji, '
              'BEZ odniesienia do ground truth. Teza pracy NIE zostanie zweryfikowana.')

    for nazwa, funkcja in metody_fp.items():
        print(f'[ANALIZA FP] Stosowanie metody: {nazwa}')
        dem_po_fp=funkcja(dem_oryginal, cellsize)

        output_path=os.path.join(output_dir, f'{base_name}_FP_{nazwa}.tif')
        zapisz_nmt(dem_po_fp, profil, output_path)
        wyniki_fp[nazwa]=output_path

        #---BLAD WZGLEDEM DANYCH SPRZED FILTRACJI (jak dotychczas - wielkosc zmiany)---
        stats=_stats_roznicy_array(dem_oryginal, dem_po_fp)
        roznice_fp[nazwa]=stats
        print(f'[ANALIZA FP] {nazwa} (vs przed filtracja): '
              f'MAE={_fmt(stats["MAE"])} | RMSE={_fmt(stats["RMSE"])}')

        maska_valid_fp=~np.isnan(dem_oryginal) & ~np.isnan(dem_po_fp)
        roznica_raster=np.full(dem_oryginal.shape, np.nan, dtype='float32')
        roznica_raster[maska_valid_fp]=(
            dem_oryginal[maska_valid_fp] - dem_po_fp[maska_valid_fp]).astype('float32')

        roznica_path=os.path.join(output_dir, f'{base_name}_ROZNICA_FP_{nazwa}.tif')
        zapisz_nmt(roznica_raster, profil, roznica_path)
        rastry_roznicowe_fp[nazwa]=roznica_path
        del roznica_raster
        print(f'[ANALIZA FP] Raster roznicowy (vs przed filtracja) zapisany w: {roznica_path}')

        #---BLAD WZGLEDEM REFERENCJI NATYWNEJ (ground truth) - TO SPRAWDZA TEZE PRACY---
        if referencja_natywna is not None:
            diff_vs_ref_path=os.path.join(
                output_dir, f'{base_name}_ROZNICA_vs_REF_FP_{nazwa}.tif')
            stats_vs_ref=raster_roznicowy(referencja_natywna, output_path, diff_vs_ref_path)
            roznice_vs_referencja[nazwa]=stats_vs_ref
            rastry_roznicowe_vs_referencja[nazwa]=diff_vs_ref_path

            delta_rmse=None
            if (roznice_vs_referencja.get('przed_filtracja') is not None
                    and roznice_vs_referencja['przed_filtracja'].get('RMSE') is not None
                    and stats_vs_ref.get('RMSE') is not None):
                delta_rmse=stats_vs_ref['RMSE'] - roznice_vs_referencja['przed_filtracja']['RMSE']
            print(f'[ANALIZA FP] {nazwa} (vs REFERENCJA NATYWNA): '
                  f'MAE={_fmt(stats_vs_ref["MAE"])} | RMSE={_fmt(stats_vs_ref["RMSE"])}'
                  + (f' | delta RMSE={delta_rmse:+.4f} m '
                     f'({"POPRAWA" if delta_rmse < 0 else "POGORSZENIE" if delta_rmse > 0 else "BEZ ZMIAN"})'
                     if delta_rmse is not None else ''))

        cva_fp[nazwa]=sygnatura_skalowa_cva(dem_po_fp, cellsize, rozmiary_okien_cva)

        #---MAPA RASTROWA CVA DLA WYNIKU TEJ METODY FILTRACJI---
        print(f'[ANALIZA FP] Zapis mapy rastrowej CVA dla {nazwa} (okno {cva_raster_okno} px)...')
        cva_mapa=oblicz_cva(dem_po_fp, cellsize, rozmiar_okna=cva_raster_okno)
        cva_mapa_path=os.path.join(
            output_dir, f'{base_name}_CVA_okno{cva_raster_okno}_{nazwa}.tif')
        zapisz_nmt(cva_mapa, profil, cva_mapa_path)
        cva_rastry[nazwa]=cva_mapa_path
        del cva_mapa

    wynik={'wyniki_fp': wyniki_fp,
             'roznice_fp': roznice_fp,
             'rastry_roznicowe_fp': rastry_roznicowe_fp,
             'cva_oryginal': cva_oryginal,
             'cva_fp': cva_fp,
             'cva_rastry': cva_rastry,
             'roznice_vs_referencja': roznice_vs_referencja,
             'rastry_roznicowe_vs_referencja': rastry_roznicowe_vs_referencja,}

    #---BEZ AUTOMATYCZNEGO RAPORTU TUTAJ (dubluje sie z zapisz_raport_fp w wersja_dev.py)---
    return wynik# -*- coding: utf-8 -*-
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
from fp_filters import fp_methods, sygnatura_skalowa_cva, oblicz_cva, wczytaj_nmt, zapisz_nmt


def _fmt(wartosc, jednostka=' m', miejsca=3):
    #---BEZPIECZNE FORMATOWANIE LICZBY (zwraca '-' zamiast rzucac TypeError na None)---
    if wartosc is None:
        return '-'
    return f'{wartosc:.{miejsca}f}{jednostka}'


def _round_cva_dict(cva_dict, miejsca=3):
    #---ZAOKRAGLA WARTOSCI CVA (KLUCZ=ROZMIAR OKNA, WARTOSC=CVA) DO 'miejsca' MIEJSC---
    return {okno: (round(wartosc, miejsca) if wartosc is not None else None)
            for okno, wartosc in cva_dict.items()}


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

    #---BLAD WZGLEDEM DANYCH SPRZED FILTRACJI (wielkosc zmiany wprowadzonej przez filtr)---
    linie.append('=== BLAD WZGLEDEM DANYCH SPRZED FILTRACJI (input vs output filtra) ===')
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

    #---BLAD WZGLEDEM REFERENCJI NATYWNEJ (ground truth) - WERYFIKACJA TEZY PRACY---
    #czy (resampling + filtracja) przyblizza wynik do prawdy bardziej niz sam resampling?
    #TO JEST KLUCZOWE: bez tego bloku praca nigdy nie sprawdza wlasnej hipotezy badawczej
    roznice_vs_ref=wyniki_fp.get('roznice_vs_referencja', {})
    if roznice_vs_ref:
        linie.append('=== BLAD WZGLEDEM REFERENCJI NATYWNEJ (ground truth) ===')
        linie.append('(czy filtracja przyblizyla wynik do prawdziwej rzezby terenu,')
        linie.append(' a nie tylko zmienila dane wzgledem samych siebie)')
        linie.append('')

        stats_wejscie=roznice_vs_ref.get('przed_filtracja')
        if stats_wejscie is not None:
            linie.append('---PRZED FILTRACJA (sam resampling, punkt odniesienia)---')
            linie.append(f"liczba pikseli (n_px): {stats_wejscie.get('n_px')}")
            linie.append(f"MAE ={_fmt(stats_wejscie.get('MAE'))}")
            linie.append(f"RMSE={_fmt(stats_wejscie.get('RMSE'))}")
            linie.append('')

        for nazwa, stats in roznice_vs_ref.items():
            if nazwa == 'przed_filtracja':
                continue
            linie.append(f'---METODA {nazwa.upper()} (po filtracji)---')
            linie.append(f"liczba pikseli (n_px): {stats.get('n_px')}")
            linie.append(f"MAE ={_fmt(stats.get('MAE'))}")
            linie.append(f"RMSE={_fmt(stats.get('RMSE'))}")

            if stats_wejscie is not None and stats_wejscie.get('RMSE') is not None and stats.get('RMSE') is not None:
                delta_rmse=stats['RMSE'] - stats_wejscie['RMSE']
                kierunek='POPRAWA' if delta_rmse < 0 else ('POGORSZENIE' if delta_rmse > 0 else 'BEZ ZMIAN')
                linie.append(f"delta RMSE vs przed filtracja: {delta_rmse:+.4f} m ({kierunek})")
            linie.append('')
    else:
        linie.append('=== BLAD WZGLEDEM REFERENCJI NATYWNEJ: NIE OBLICZONO ===')
        linie.append('(nie podano referencji natywnej przy wywolaniu analiza_fp_generalizacji)')
        linie.append('')

    linie.append('---SYGNATURA SKALOWA CVA (zaokraglone do 3 msc po przecinku)---')
    cva_oryginal=_round_cva_dict(wyniki_fp.get('cva_oryginal', {}))
    linie.append(f"ORYGINAL: {cva_oryginal}")
    for nazwa, cva in wyniki_fp.get('cva_fp', {}).items():
        linie.append(f"{nazwa.upper()}: {_round_cva_dict(cva)}")
    linie.append('')

    #---MAPY RASTROWE CVA (przestrzenny rozklad szorstkosci terenu)---
    cva_rastry=wyniki_fp.get('cva_rastry', {})
    if cva_rastry:
        linie.append('---MAPY RASTROWE CVA (zapisane pliki .tif)---')
        for nazwa, sciezka in cva_rastry.items():
            linie.append(f"{nazwa.upper()}: {sciezka}")
        linie.append('')

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    report_path=os.path.join(output_dir, f'{base_name}_RAPORT_FP.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(linie))

    print(f'[ANALIZA FP] Raport zapisano w: {report_path}')
    return report_path


def analiza_fp_generalizacji(dem_path, output_dir, base_name, metody_fp=None,
                             rozmiary_okien_cva=(3, 5, 7, 9, 11, 15, 21),
                             referencja_natywna=None, cva_raster_okno=9):
    '''
    referencja_natywna : sciezka do rastra ground truth (najdrobniejszy piksel
        wsrod arkuszy zrodlowych, patrz zbuduj_referencje_natywna w nfp_mosaics.py).

        !!!KLUCZOWE!!!: bez podania tego argumentu funkcja liczy WYLACZNIE blad
        filtracji wzgledem samej siebie (ile filtr zmienil dane), a NIE sprawdza
        czy filtracja faktycznie przyblizyla wynik do prawdziwej rzezby terenu.
        Teza pracy ("filtracja feature-preserving po resamplingu daje model o
        WYZSZEJ WIARYGODNOSCI GEOMETRYCZNEJ niz sam resampling") wymaga
        porownania WZGLEDEM REFERENCJI, nie tylko przed/po filtracji miedzy soba.
        Podaj referencja_natywna zawsze, gdy jest dostepna (patrz
        analiza_wielorozdzielczosciowa - ten sam plik, ktory tam sluzy jako
        ground truth do oceny bledu resamplingu).

    cva_raster_okno : rozmiar okna (w pikselach), dla ktorego zapisywana jest
        PRZESTRZENNA MAPA CVA (raster .tif, wartosci 0-1 na kazdym pikselu),
        w odroznieniu od sygnatury_skalowa_cva, ktora zwraca tylko SREDNIA
        wartosc CVA (jedna liczba) na cale zobrazowanie, dla kilku rozmiarow
        okna naraz. Mapa rastrowa pozwala zobaczyc GDZIE dana metoda splaszcza
        teren, a nie tylko O ILE (w agregacie). Domyslnie generowana jest
        TYLKO dla jednego rozmiaru okna (zeby nie mnozyc plikow x7 rozmiarow),
        wybierz wartosc reprezentatywna dla analizy (np. srodek zakresu
        rozmiary_okien_cva).
    '''
    if metody_fp is None:
        metody_fp=fp_methods

    dem_oryginal, cellsize, profil=wczytaj_nmt(dem_path)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    wyniki_fp={}
    roznice_fp={}
    rastry_roznicowe_fp={}
    cva_fp={}
    cva_rastry={}
    roznice_vs_referencja={}
    rastry_roznicowe_vs_referencja={}

    print(f'[ANALIZA FP] Sygnatura CVA oryginalu (rozdzielczosc {cellsize} m)...')
    cva_oryginal=sygnatura_skalowa_cva(dem_oryginal, cellsize, rozmiary_okien_cva)

    #---MAPA RASTROWA CVA DLA ORYGINALU (przed jakakolwiek filtracja)---
    print(f'[ANALIZA FP] Zapis mapy rastrowej CVA oryginalu (okno {cva_raster_okno} px)...')
    cva_mapa_oryginal=oblicz_cva(dem_oryginal, cellsize, rozmiar_okna=cva_raster_okno)
    cva_mapa_oryginal_path=os.path.join(
        output_dir, f'{base_name}_CVA_okno{cva_raster_okno}_oryginal.tif')
    zapisz_nmt(cva_mapa_oryginal, profil, cva_mapa_oryginal_path)
    cva_rastry['oryginal']=cva_mapa_oryginal_path
    del cva_mapa_oryginal

    #---BLAD WZGLEDEM REFERENCJI NATYWNEJ, DLA DANYCH SPRZED FILTRACJI---
    #to jest PUNKT ODNIESIENIA: samo MAE/RMSE resamplingu wzgledem ground truth,
    #z ktorym porownamy pozniej kazda z trzech metod filtracji
    if referencja_natywna is not None:
        print(f'[ANALIZA FP] Blad WEJSCIA (przed filtracja) wzgledem referencji natywnej...')
        diff_wejscie_path=os.path.join(
            output_dir, f'{base_name}_ROZNICA_vs_REF_przed_filtracja.tif')
        stats_wejscie=raster_roznicowy(referencja_natywna, dem_path, diff_wejscie_path)
        roznice_vs_referencja['przed_filtracja']=stats_wejscie
        rastry_roznicowe_vs_referencja['przed_filtracja']=diff_wejscie_path
        print(f'[ANALIZA FP] przed_filtracja vs REF: '
              f'MAE={_fmt(stats_wejscie["MAE"])} | RMSE={_fmt(stats_wejscie["RMSE"])}')
    else:
        print('[ANALIZA FP] UWAGA: referencja_natywna nie podana - blad filtracji '
              'zostanie policzony WYLACZNIE wzgledem danych sprzed filtracji, '
              'BEZ odniesienia do ground truth. Teza pracy NIE zostanie zweryfikowana.')

    for nazwa, funkcja in metody_fp.items():
        print(f'[ANALIZA FP] Stosowanie metody: {nazwa}')
        dem_po_fp=funkcja(dem_oryginal, cellsize)

        output_path=os.path.join(output_dir, f'{base_name}_FP_{nazwa}.tif')
        zapisz_nmt(dem_po_fp, profil, output_path)
        wyniki_fp[nazwa]=output_path

        #---BLAD WZGLEDEM DANYCH SPRZED FILTRACJI (jak dotychczas - wielkosc zmiany)---
        stats=_stats_roznicy_array(dem_oryginal, dem_po_fp)
        roznice_fp[nazwa]=stats
        print(f'[ANALIZA FP] {nazwa} (vs przed filtracja): '
              f'MAE={_fmt(stats["MAE"])} | RMSE={_fmt(stats["RMSE"])}')

        maska_valid_fp=~np.isnan(dem_oryginal) & ~np.isnan(dem_po_fp)
        roznica_raster=np.full(dem_oryginal.shape, np.nan, dtype='float32')
        roznica_raster[maska_valid_fp]=(
            dem_oryginal[maska_valid_fp] - dem_po_fp[maska_valid_fp]).astype('float32')

        roznica_path=os.path.join(output_dir, f'{base_name}_ROZNICA_FP_{nazwa}.tif')
        zapisz_nmt(roznica_raster, profil, roznica_path)
        rastry_roznicowe_fp[nazwa]=roznica_path
        del roznica_raster
        print(f'[ANALIZA FP] Raster roznicowy (vs przed filtracja) zapisany w: {roznica_path}')

        #---BLAD WZGLEDEM REFERENCJI NATYWNEJ (ground truth) - TO SPRAWDZA TEZE PRACY---
        if referencja_natywna is not None:
            diff_vs_ref_path=os.path.join(
                output_dir, f'{base_name}_ROZNICA_vs_REF_FP_{nazwa}.tif')
            stats_vs_ref=raster_roznicowy(referencja_natywna, output_path, diff_vs_ref_path)
            roznice_vs_referencja[nazwa]=stats_vs_ref
            rastry_roznicowe_vs_referencja[nazwa]=diff_vs_ref_path

            delta_rmse=None
            if (roznice_vs_referencja.get('przed_filtracja') is not None
                    and roznice_vs_referencja['przed_filtracja'].get('RMSE') is not None
                    and stats_vs_ref.get('RMSE') is not None):
                delta_rmse=stats_vs_ref['RMSE'] - roznice_vs_referencja['przed_filtracja']['RMSE']
            print(f'[ANALIZA FP] {nazwa} (vs REFERENCJA NATYWNA): '
                  f'MAE={_fmt(stats_vs_ref["MAE"])} | RMSE={_fmt(stats_vs_ref["RMSE"])}'
                  + (f' | delta RMSE={delta_rmse:+.4f} m '
                     f'({"POPRAWA" if delta_rmse < 0 else "POGORSZENIE" if delta_rmse > 0 else "BEZ ZMIAN"})'
                     if delta_rmse is not None else ''))

        cva_fp[nazwa]=sygnatura_skalowa_cva(dem_po_fp, cellsize, rozmiary_okien_cva)

        #---MAPA RASTROWA CVA DLA WYNIKU TEJ METODY FILTRACJI---
        print(f'[ANALIZA FP] Zapis mapy rastrowej CVA dla {nazwa} (okno {cva_raster_okno} px)...')
        cva_mapa=oblicz_cva(dem_po_fp, cellsize, rozmiar_okna=cva_raster_okno)
        cva_mapa_path=os.path.join(
            output_dir, f'{base_name}_CVA_okno{cva_raster_okno}_{nazwa}.tif')
        zapisz_nmt(cva_mapa, profil, cva_mapa_path)
        cva_rastry[nazwa]=cva_mapa_path
        del cva_mapa

    wynik={'wyniki_fp': wyniki_fp,
             'roznice_fp': roznice_fp,
             'rastry_roznicowe_fp': rastry_roznicowe_fp,
             'cva_oryginal': cva_oryginal,
             'cva_fp': cva_fp,
             'cva_rastry': cva_rastry,
             'roznice_vs_referencja': roznice_vs_referencja,
             'rastry_roznicowe_vs_referencja': rastry_roznicowe_vs_referencja,}

    #---BEZ AUTOMATYCZNEGO RAPORTU TUTAJ (dubluje sie z zapisz_raport_fp w wersja_dev.py)---
    return wynik