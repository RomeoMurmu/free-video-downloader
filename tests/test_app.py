import app as downloader


def test_robots_and_sitemap_are_available():
    client = downloader.app.test_client()
    robots = client.get("/robots.txt")
    sitemap = client.get("/sitemap.xml")
    assert robots.status_code == 200
    assert "Sitemap: /sitemap.xml" in robots.text
    assert sitemap.status_code == 200
    assert "<urlset" in sitemap.text

def test_validate_url_accepts_http_and_https():
    assert downloader.validate_url("https://example.com/video")
    assert downloader.validate_url("http://example.com/video")


def test_validate_url_rejects_unsafe_or_malformed_urls():
    assert not downloader.validate_url("javascript:alert(1)")
    assert not downloader.validate_url("file:///etc/passwd")
    assert not downloader.validate_url("https://user:pass@example.com/video")
    assert not downloader.validate_url("not a url")


def test_file_endpoint_rejects_path_traversal():
    client = downloader.app.test_client()
    response = client.get("/get-file/../app.py")
    assert response.status_code in (400, 404)


def test_info_rejects_invalid_url():
    client = downloader.app.test_client()
    response = client.post("/info", json={"url": "file:///etc/passwd"})
    assert response.status_code == 400
    assert response.json["success"] is False


def test_formats_are_accurate_and_sorted():
    info = {
        "formats": [
            {"height": 360, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
            {"height": 1080, "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4", "filesize": 2048},
            {"height": 720, "vcodec": "avc1", "acodec": "none", "ext": "webm"},
        ]
    }
    result = downloader.build_formats(info)
    assert [item["quality"] for item in result[:3]] == ["1080p", "720p", "360p"]
    assert result[0]["has_audio"] is True
    assert result[0]["filesize"] == "2.0 KiB"


def test_cleanup_file_removes_only_files_inside_download_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "DOWNLOAD_FOLDER", tmp_path)
    target = tmp_path / "job-video.mp4"
    target.write_bytes(b"video")
    assert downloader.cleanup_file(target) is True
    assert not target.exists()


def test_cleanup_file_does_not_remove_outside_file(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    monkeypatch.setattr(downloader, "DOWNLOAD_FOLDER", download_dir)
    assert downloader.cleanup_file(outside) is False
    assert outside.exists()
def test_get_file_stream_deletes_file_after_response_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "DOWNLOAD_FOLDER", tmp_path)
    target = tmp_path / "job-video.mp4"
    target.write_bytes(b"video-data")
    client = downloader.app.test_client()
    response = client.get("/get-file/job-video.mp4")
    assert response.status_code == 200
    assert response.data == b"video-data"
    assert not target.exists()


def test_cleanup_endpoint_deletes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "DOWNLOAD_FOLDER", tmp_path)
    target = tmp_path / "job-video.mp4"
    target.write_bytes(b"video-data")
    client = downloader.app.test_client()
    response = client.delete("/cleanup/job-video.mp4")
    assert response.status_code == 200
    assert response.json == {"success": True, "deleted": True}
    assert not target.exists()


def test_download_endpoint_returns_background_job(monkeypatch):
    monkeypatch.setattr(downloader, "download_worker", lambda *args: None)
    client = downloader.app.test_client()
    response = client.post("/download-format", json={"url": "https://example.com/video", "quality": "720p", "type": "video"})
    assert response.status_code == 202
    assert response.json["success"] is True
    assert response.json["job_id"]

