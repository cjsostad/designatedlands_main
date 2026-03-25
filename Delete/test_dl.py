from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen
from zipfile import ZipFile


ZIP_URL = (
    "https://data-donnees.az.ec.gc.ca/api/file?path=%2Fspecies%2Fprotectrestore"
    "%2Fcritical-habitat-species-at-risk-canada%2FCriticalHabitat.zip"
)


def download_zip(url: str, destination: Path) -> None:
    with urlopen(url) as response, destination.open("wb") as output_file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output_file.write(chunk)


def extract_zip(zip_path: Path, destination: Path) -> None:
    with ZipFile(zip_path, "r") as zip_file:
        zip_file.extractall(destination)


def main() -> None:
    root_dir = Path(__file__).resolve().parent
    print(f"Downloading ZIP to temporary file from: {ZIP_URL}")

    temp_path = None
    try:
        with NamedTemporaryFile(delete=False, suffix=".zip") as temp_file:
            temp_path = Path(temp_file.name)

        download_zip(ZIP_URL, temp_path)
        print(f"Extracting contents to: {root_dir}")
        extract_zip(temp_path, root_dir)
        print("Download and extraction complete.")
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


if __name__ == "__main__":
    main()