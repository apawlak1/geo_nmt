# -*- coding: utf-8 -*-
'''
FUNKCJE GENERALIZACJI NMT METODAMI FEATURE-PRESERVING (FP)
Odpowiednik nfp_mosaics.py dla metod Feature-preserving

1. fpdems: Feature-Preserving DEM Smoothing (wg: https://www.mdpi.com/2072-4292/11/16/1926),
    implementacja przez WhiteboxTools
3. guided_filter_dem: edge-preserving wygladzanie z zamknietym rozwiazaniem liniowym
4. rekonstrukcja_morfologiczna: wygladzanie morfologiczne przez rekonstrukcje
    "The effect of morphological smoothening by reconstruction on the extraction
    of peaks and pits from digital elevation models"
'''

import os
import tempfile
import numpy as np
from scipy.ndimage import uniform_filter, grey_opening
import rasterio
from rasterio.transform import from_origin
from rasterio.crs import CRS
import whitebox
from skimage.morphology import reconstruction

#---INICJALIZACJA WHITEBOXTOOLS (raz, przy imporcie modulu)---
WBT=whitebox.WhiteboxTools()
WBT.set_verbose_mode(False)

#---1. FPDEMS (Feature-Preserving DEM Smoothing): WhiteboxTools---
def fpdems(dem: np.ndarray, cellsize: float, theta_t_deg: float=15.0,
           iteracje_normalnych: int=5, max_diff: float=0.5) -> np.ndarray:
    nodata_mask=np.isnan(dem)
    NODATA_WBT=-32768.0     #dolna granica int16, dla wbt 'no data'
    dem_in=np.where(nodata_mask, NODATA_WBT, dem).astype('float32')

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path=os.path.join(tmpdir, 'dem_in.tif')
        out_path=os.path.join(tmpdir, 'dem_out.tif')

        transform=from_origin(0.0, dem.shape[0] * cellsize, cellsize, cellsize)
        profil={'driver': 'GTiff', 'height': dem.shape[0], 'width': dem.shape[1],
                'count': 1, 'dtype': 'float32', 'crs': CRS.from_epsg(2180),
                'transform': transform, 'nodata': NODATA_WBT}
        with rasterio.open(in_path, 'w', **profil) as dst:
            dst.write(dem_in, 1)

        wynik_wbt=WBT.feature_preserving_smoothing(
            dem=in_path, output=out_path,
            filter=11, norm_diff=theta_t_deg,
            num_iter=iteracje_normalnych, max_diff=max_diff)

        if wynik_wbt != 0 or not os.path.exists(out_path):
            raise RuntimeError(f'whitebox feature_preserving_smoothing zwrocilo blad (kod {wynik_wbt})')

        with rasterio.open(out_path) as src:
            dem_wygladzony=src.read(1).astype('float64')

    dem_wygladzony[dem_wygladzony == NODATA_WBT]=np.nan
    dem_wygladzony[nodata_mask]=np.nan
    return dem_wygladzony


#---4. GUIDED FILTER (He, Sun, Tang 2010/2013)---
def guided_filter_dem(dem: np.ndarray, cellsize: float=None,
                      radius: int=4, eps: float=1.0) -> np.ndarray:
    '''
    Guided Filter w wariancie "self-guided" (obraz prowadzacy I = wygladzany
    obraz p = ten sam DEM) - dziala jak filtr bilateralny (wygladza plaskie
    obszary, zachowuje krawedzie), ale ma zamkniete, liniowe rozwiazanie
    (kilka box-filterow zamiast kosztownego jadra zaleznego od wartosci w
    kazdym pikselu), wiec jego koszt NIE rosnie z rozmiarem okna tak, jak
    w filtrze bilateralnym.

    Parametr 'cellsize'  tu nieuzywany.
    Zachowany tylko dla zgodnosci sygnatury z pozostalymi metodami w fp_methods
    (wywolywane jednolicie jako funkcja(dem, cellsize)).

    OZNACZENIA:
    (a = waga "zachowaj oryginal", w [0,1]):
    +mean_I, mean_p   - srednie lokalne (box filter o promieniu 'radius')
    +var_I            - lokalna wariancja obrazu I (tu: samego DEM)
    +a = var_I / (var_I + eps)   -> a~1 przy duzej lokalnej wariancji
        (krawedz terenu - piksel prawie niezmieniony),
        a~0 przy malej wariancji (obszar plaski - piksel zastapiony lokalna srednia)
    +b = mean_p - a * mean_I
    +q = mean(a)*I + mean(b) ---> koncowe wygladzenie z zachowaniem krawedzi

    'eps' (epsilon) steruje czuloscia na krawedzie:
    mniejsze eps = mniej wygladzania (wiecej krawedzi wykrytych)
    wieksze eps = bardziej ogolne wygladzanie
    '''
    img=dem.astype(np.float64, copy=True)
    nodata_mask=np.isnan(img)
    if nodata_mask.any():
        img=np.where(nodata_mask, np.nanmean(img), img)

    rozmiar_okna=2 * radius + 1

    mean_I=uniform_filter(img, size=rozmiar_okna)
    corr_I=uniform_filter(img * img, size=rozmiar_okna)
    var_I=corr_I - mean_I ** 2

    #---self-guided: I == p, wiec cov_Ip == var_I i mean_p == mean_I---
    a=var_I / (var_I + eps)
    b=mean_I - a * mean_I

    mean_a=uniform_filter(a, size=rozmiar_okna)
    mean_b=uniform_filter(b, size=rozmiar_okna)

    wynik=mean_a * img + mean_b

    if nodata_mask.any():
        wynik[nodata_mask]=np.nan

    return wynik

#---5. WYGLADZANIE MORFOLOGICZNE PRZEZ REKONSTRUKCJE---
def rekonstrukcja_morfologiczna(dem: np.ndarray, cellsize: float=None,
                                rozmiar_okna: int=5) -> np.ndarray:
    '''
    Usuwa SZTUCZNE (nie majace odpowiednika w rzeczywistej rzezbie)
    pojedyncze szczyty i zaglebienia (np. artefakty resamplingu/interpolacji)

    Usuwanie szczytow (peaks):
        1. otwarcie morfologiczne (erozja+dylatacja) w oknie 'rozmiar_okna'
           obcina wszystkie lokalne szczyty i TEZ nienaturalnie poszerza doliny
        2. rekonstrukcja geodezyjna przez dylatacje:
           uzywajac otwartego NMT jako markera (seed) i oryginalnego NMT jako maski:
           odbudowuje kazda forme terenu, ktora przetrwala krok 1
           NIE odbudowuje pojedynczych sztucznych szczytow
           (bo zniknely w kroku 1 i rekonstrukcja nie przywraca czegos
           co marker w ogole juz nie zawiera)
        3. to samo z zaglebieniami ale na odwroconym NMT
    '''
    img=dem.astype(np.float64, copy=True)
    nodata_mask=np.isnan(img)
    if nodata_mask.any():
        img=np.where(nodata_mask, np.nanmean(img), img)

    footprint=np.ones((rozmiar_okna, rozmiar_okna))

    #---USUWANIE SZTUCZNYCH SZCZYTOW---
    otwarty=grey_opening(img, footprint=footprint)
    bez_szczytow=reconstruction(otwarty, img, method='dilation')

    #---USUWANIE SZTUCZNYCH ZAGLEBIEN (na odwroconym, juz oczyszczonym z gory rastrze)---
    odwrocony=-bez_szczytow
    otwarty_odwrocony=grey_opening(odwrocony, footprint=footprint)
    bez_dolkow_odwrocony=reconstruction(otwarty_odwrocony, odwrocony, method='dilation')

    wynik=-bez_dolkow_odwrocony

    if nodata_mask.any():
        wynik[nodata_mask]=np.nan

    return wynik


#---METRYKA OCENY: CVA (Circular Variance of Aspect)---
def oblicz_aspekt(dem: np.ndarray, cellsize: float) -> np.ndarray:
    dem32=dem.astype(np.float32, copy=False)
    dzdy, dzdx=np.gradient(dem32, np.float32(cellsize))
    aspekt=np.arctan2(-dzdy, -dzdx).astype(np.float32, copy=False)
    aspekt=np.mod(aspekt, np.float32(2 * np.pi))
    aspekt[np.isnan(dem)]=np.nan
    return aspekt


def oblicz_cva(dem: np.ndarray, cellsize: float, rozmiar_okna: int=3) -> np.ndarray:
    aspekt=oblicz_aspekt(dem, cellsize)
    maska_valid=~np.isnan(aspekt)

    cos_a=np.where(maska_valid, np.cos(aspekt), 0.0).astype(np.float32, copy=False)
    sin_a=np.where(maska_valid, np.sin(aspekt), 0.0).astype(np.float32, copy=False)
    waga=maska_valid.astype(np.float32)

    suma_cos=uniform_filter(cos_a, size=rozmiar_okna, mode="nearest")
    suma_sin=uniform_filter(sin_a, size=rozmiar_okna, mode="nearest")
    liczba_valid=uniform_filter(waga, size=rozmiar_okna, mode="nearest")

    with np.errstate(invalid="ignore", divide="ignore"):
        srednia_cos=suma_cos / liczba_valid
        srednia_sin=suma_sin / liczba_valid

    R=np.sqrt(srednia_cos ** 2 + srednia_sin ** 2)
    cva=1.0 - R

    cva[liczba_valid == 0]=np.nan
    cva[np.isnan(dem)]=np.nan
    return cva


def sygnatura_skalowa_cva(dem: np.ndarray, cellsize: float,
                          rozmiary_okien=(3, 5, 7, 9, 11, 15, 21)) -> dict:

    aspekt=oblicz_aspekt(dem, cellsize)
    maska_valid=~np.isnan(aspekt)
    dem_nan_mask=np.isnan(dem)

    cos_a=np.where(maska_valid, np.cos(aspekt), 0.0).astype(np.float32, copy=False)
    sin_a=np.where(maska_valid, np.sin(aspekt), 0.0).astype(np.float32, copy=False)
    waga=maska_valid.astype(np.float32)
    del aspekt, maska_valid  #---juz niepotrzebne, zwalniam PRZED petla po oknach---

    wyniki={}
    for r in rozmiary_okien:
        suma_cos=uniform_filter(cos_a, size=r, mode="nearest")
        suma_sin=uniform_filter(sin_a, size=r, mode="nearest")
        liczba_valid=uniform_filter(waga, size=r, mode="nearest")

        with np.errstate(invalid="ignore", divide="ignore"):
            srednia_cos=suma_cos / liczba_valid
            srednia_sin=suma_sin / liczba_valid

        R=np.sqrt(srednia_cos ** 2 + srednia_sin ** 2)
        cva=1.0 - R
        cva[liczba_valid == 0]=np.nan
        cva[dem_nan_mask]=np.nan

        wyniki[r]=float(np.nanmean(cva))

        #---ZWALNIAM TABLICE TEJ ITERACJI ZANIM ZACZNE KOLEJNA---
        del suma_cos, suma_sin, liczba_valid, srednia_cos, srednia_sin, R, cva

    return wyniki


#---WCZYTANIE I ZAPIS RASTRA---
def wczytaj_nmt(sciezka: str):
    with rasterio.open(sciezka) as src:
        dem=src.read(1).astype(np.float32)
        profil=src.profile.copy()
        nodata=src.nodata
        if nodata is not None:
            dem[dem == nodata]=np.nan
        cellsize=src.res[0]
    return dem, cellsize, profil


def zapisz_nmt(dem: np.ndarray, profil: dict, sciezka: str):
    profil=profil.copy()
    profil.update(dtype="float32", nodata=np.nan)
    #---ZAOKRAGLENIE WYSOKOSCI DO 2 MSC PO PRZECINKU---
    #spojnie z konwencja przyjeta w ASCII2GT.py/AIAG2GT.py przy wczytywaniu
    #danych wejsciowych - metody FP (fpdems/guided_filter/rekonstrukcja
    #morfologiczna) same w sobie nie zaokraglaja wyniku (whitebox/scipy/
    #skimage zwracaja pelna precyzje float), wiec bez tego zaokraglenia
    #plik wyjsciowy mial nieuzyteczna liczbe miejsc po przecinku
    dem=np.round(dem, 2)
    with rasterio.open(sciezka, "w", **profil) as dst:
        dst.write(dem.astype(np.float32), 1)


#---METODY FP (analogicznie do nfp_methods w nfp_mosaics.py)---
#uzywane w analiza.py do policzenia bledow i porownania ich miedzy soba
fp_methods={'fpdems': lambda dem, cellsize: fpdems(dem, cellsize),
            'guided_filter': lambda dem, cellsize: guided_filter_dem(dem, cellsize),
            'rekonstrukcja_morfologiczna': lambda dem, cellsize: rekonstrukcja_morfologiczna(dem, cellsize),}