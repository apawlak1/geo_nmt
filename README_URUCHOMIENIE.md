# Uruchomienie narzedzia NMT

Rekomendowane srodowisko: **Miniconda / conda-forge**.

Projekt korzysta z bibliotek GIS (`geopandas`, `rasterio`, `fiona`, `pyproj`), ktore na Windowsie najpewniej instaluja sie przez conda-forge. W repozytorium sa pliki:

- `environment.yml` - rekomendowana instalacja przez Miniconda/Conda,
- `requirements.txt` - alternatywa dla `pip`.

## Wersja Pythona

Rekomendowana wersja: **Python 3.9**.

W projekcie widoczne sa pliki cache `__pycache__` dla Pythona 3.9, dlatego ta wersja jest najbezpieczniejszym wyborem. Python 3.10 tez prawdopodobnie bedzie dzialal, ale 3.9 jest domyslnie wpisany w `environment.yml`.

## Instalacja przez Miniconda

1. Zainstaluj Miniconda.
2. Otworz terminal Anaconda Prompt albo PowerShell.
3. Przejdz do folderu projektu:

```powershell
cd C:\SkryptyPython\Badania\NMT_apawlak\geo_nmt
```

4. Utworz srodowisko:

```powershell
conda env create -f environment.yml
```

5. Aktywuj srodowisko:

```powershell
conda activate geo-nmt
```

## Instalacja przez pip

Wariant `pip` moze byc trudniejszy na Windowsie, szczegolnie dla `geopandas`, `rasterio` i `fiona`.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Glowne uruchomienie

Najpelniejszy przeplyw uruchamia skrypt:

```powershell
python folium_v3.py
```

Skrypt pyta w konsoli o:

- nazwe powiatu,
- rok albo lata,
- czy laczyc arkusze w mozaike,
- czy pobrac dane.

Wyniki sa zapisywane w folderze cache ustawionym w skrypcie `folium_v3.py`.

## Sama mapa skorowidzow

Do wygenerowania mapy na podstawie `config.json` mozna uzyc:

```powershell
python mapa_folium.py
```

Ten wariant korzysta z sekcji `mapa_folium` w pliku `config.json`.

## Konwersja TIN

Skrypt `konwersja_tin.py` wymaga biblioteki `arcpy`, czyli srodowiska ArcGIS Pro. `arcpy` nie jest dodane do `environment.yml` ani `requirements.txt`, bo nie instaluje sie standardowo przez `pip`/conda-forge.

Uruchamiaj ten skrypt z Pythonem dostarczanym przez ArcGIS Pro, jezeli potrzebujesz konwersji ESRI TIN:

```powershell
python konwersja_tin.py
```

## Uwaga o sciezkach

Czesc sciezek jest wpisana wprost w skryptach albo w `config.json`, np. folder cache. Przed uruchomieniem na innym komputerze warto sprawdzic:

- `config.json`,
- zmienna `cache_dir` w `folium_v3.py`,
- sciezki testowe w skryptach konwersji.
