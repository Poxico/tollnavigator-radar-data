# TollNavigator radar data

Repozytorium automatycznie generuje bazę fotoradarów i odcinkowych pomiarów prędkości z OpenStreetMap dla aplikacji TollNavigator.

## Pliki publiczne

Po włączeniu GitHub Pages w trybie **GitHub Actions** aplikacja pobiera:

```text
https://TWOJ_LOGIN.github.io/tollnavigator-radar-data/tollnavigator/speed_cameras_meta.json
https://TWOJ_LOGIN.github.io/tollnavigator-radar-data/tollnavigator/speed_cameras.json
```

## Automatyzacja

Workflow `.github/workflows/update-speed-cameras.yml` uruchamia się co 3 dni i ręcznie przez **Run workflow**.

Proces:

1. pobiera dane z OSM przez generator,
2. tworzy `speed_cameras.json`,
3. waliduje wynik,
4. tworzy `speed_cameras_meta.json`,
5. zapisuje pliki w repozytorium,
6. publikuje zawartość folderu `public/` przez GitHub Pages.

Jeśli walidacja się nie powiedzie, workflow kończy się błędem i nie publikuje wadliwej bazy.

## GitHub Pages

W repozytorium ustaw:

```text
Settings → Pages → Build and deployment
Source: GitHub Actions
Save
```

## Ręczne uruchomienie

```text
Actions → Update TollNavigator speed cameras → Run workflow
```
