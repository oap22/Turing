"""Tests for the FileSystemTool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from turing.tools.filesystem import FileSystemTool


@pytest.fixture()
def fs_tool(tmp_path: Path) -> FileSystemTool:
    """Create a FileSystemTool with tmp_path as the only allowed write path."""
    config = MagicMock()
    config.allowed_write_paths = [str(tmp_path)]
    return FileSystemTool(config=config)


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    """Create a sample file for read tests."""
    f = tmp_path / "sample.txt"
    f.write_text("Hello, Turing!\nLine 2\nLine 3\n")
    return f


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    """Create a sample directory structure for list and search tests."""
    sub = tmp_path / "subdir"
    sub.mkdir()
    (tmp_path / "file_a.txt").write_text("content a")
    (tmp_path / "file_b.py").write_text("content b")
    (sub / "nested.txt").write_text("nested content")
    (sub / "nested.py").write_text("nested python")
    return tmp_path


class TestReadFile:
    """Test the read_file action."""

    async def test_read_existing_file(self, fs_tool: FileSystemTool, sample_file: Path):
        """Test reading an existing file."""
        result = await fs_tool.execute(action="read_file", path=str(sample_file))
        assert result.success is True
        assert "Hello, Turing!" in result.output
        assert "Line 2" in result.output

    async def test_read_nonexistent_file(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test reading a file that does not exist."""
        result = await fs_tool.execute(action="read_file", path=str(tmp_path / "nope.txt"))
        assert result.success is False
        assert "not found" in result.error.lower()

    async def test_read_directory_fails(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test that reading a directory returns an error."""
        result = await fs_tool.execute(action="read_file", path=str(tmp_path))
        assert result.success is False
        assert "not a file" in result.error.lower()

    async def test_read_large_file_rejected(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test that files larger than 100KB are rejected."""
        large = tmp_path / "large.bin"
        large.write_bytes(b"x" * (101 * 1024))
        result = await fs_tool.execute(action="read_file", path=str(large))
        assert result.success is False
        assert "too large" in result.error.lower()


class TestWriteFile:
    """Test the write_file action."""

    async def test_write_to_allowed_path(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test writing a file within the allowed path."""
        target = tmp_path / "output.txt"
        result = await fs_tool.execute(
            action="write_file",
            path=str(target),
            content="Written by Turing",
        )
        assert result.success is True
        assert target.read_text() == "Written by Turing"

    async def test_write_creates_parent_dirs(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test that write_file creates parent directories."""
        target = tmp_path / "deep" / "nested" / "dir" / "file.txt"
        result = await fs_tool.execute(
            action="write_file",
            path=str(target),
            content="deep write",
        )
        assert result.success is True
        assert target.exists()
        assert target.read_text() == "deep write"

    async def test_write_outside_allowed_path_denied(self, fs_tool: FileSystemTool):
        """Test that writing outside allowed paths is denied."""
        result = await fs_tool.execute(
            action="write_file",
            path="/etc/passwd",
            content="hacked",
        )
        assert result.success is False
        assert "denied" in result.error.lower()

    async def test_write_empty_content(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test writing an empty file."""
        target = tmp_path / "empty.txt"
        result = await fs_tool.execute(
            action="write_file",
            path=str(target),
            content="",
        )
        assert result.success is True
        assert target.read_text() == ""


class TestListDirectory:
    """Test the list_directory action."""

    async def test_list_directory(self, fs_tool: FileSystemTool, sample_dir: Path):
        """Test listing a directory's contents."""
        result = await fs_tool.execute(action="list_directory", path=str(sample_dir))
        assert result.success is True
        assert "file_a.txt" in result.output
        assert "file_b.py" in result.output
        assert "subdir" in result.output

    async def test_list_nonexistent_directory(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test listing a directory that does not exist."""
        result = await fs_tool.execute(
            action="list_directory",
            path=str(tmp_path / "nonexistent"),
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    async def test_list_file_as_directory(self, fs_tool: FileSystemTool, sample_file: Path):
        """Test that listing a file returns an error."""
        result = await fs_tool.execute(action="list_directory", path=str(sample_file))
        assert result.success is False
        assert "not a directory" in result.error.lower()

    async def test_list_directory_entry_types(self, fs_tool: FileSystemTool, sample_dir: Path):
        """Test that directory entries show correct types."""
        result = await fs_tool.execute(action="list_directory", path=str(sample_dir))
        assert "[dir]" in result.output
        assert "[file]" in result.output


class TestSearchFiles:
    """Test the search_files action."""

    async def test_search_all_files(self, fs_tool: FileSystemTool, sample_dir: Path):
        """Test searching for all files."""
        result = await fs_tool.execute(
            action="search_files",
            path=str(sample_dir),
            pattern="*",
        )
        assert result.success is True
        assert "file_a.txt" in result.output

    async def test_search_by_extension(self, fs_tool: FileSystemTool, sample_dir: Path):
        """Test searching by file extension."""
        result = await fs_tool.execute(
            action="search_files",
            path=str(sample_dir),
            pattern="*.py",
        )
        assert result.success is True
        assert "file_b.py" in result.output
        assert "nested.py" in result.output

    async def test_search_no_matches(self, fs_tool: FileSystemTool, sample_dir: Path):
        """Test searching with no matching files."""
        result = await fs_tool.execute(
            action="search_files",
            path=str(sample_dir),
            pattern="*.xyz",
        )
        assert result.success is True
        assert "no files" in result.output.lower()

    async def test_search_nonexistent_directory(self, fs_tool: FileSystemTool, tmp_path: Path):
        """Test searching in a directory that does not exist."""
        result = await fs_tool.execute(
            action="search_files",
            path=str(tmp_path / "nonexistent"),
            pattern="*.txt",
        )
        assert result.success is False


class TestToolProperties:
    """Test tool metadata."""

    def test_name(self, fs_tool: FileSystemTool):
        assert fs_tool.name == "filesystem"

    def test_parameters_schema(self, fs_tool: FileSystemTool):
        params = fs_tool.parameters
        assert params["type"] == "object"
        assert "action" in params["properties"]
        assert "path" in params["properties"]

    def test_risk_level(self, fs_tool: FileSystemTool):
        from turing.tools.base import RiskLevel

        assert fs_tool.risk_level == RiskLevel.MEDIUM


class TestErrorHandling:
    """Test error handling."""

    async def test_no_action(self, fs_tool: FileSystemTool):
        """Test that missing action returns an error."""
        result = await fs_tool.execute(path="/tmp")
        assert result.success is False
        assert "no action" in result.error.lower()

    async def test_no_path(self, fs_tool: FileSystemTool):
        """Test that missing path returns an error."""
        result = await fs_tool.execute(action="read_file")
        assert result.success is False
        assert "no path" in result.error.lower()

    async def test_invalid_action(self, fs_tool: FileSystemTool):
        """Test that an invalid action returns an error."""
        result = await fs_tool.execute(action="delete_everything", path="/tmp")
        assert result.success is False
        assert "unknown action" in result.error.lower()
