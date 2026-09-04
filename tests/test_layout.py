import pytest

from hisiburn.layout import (
    BUILTIN_LAYOUTS,
    ERASE_BLOCK,
    FlashLayout,
    LayoutError,
    Partition,
    get_layout,
    round_up,
)


def test_round_up_to_erase_block():
    assert round_up(1) == ERASE_BLOCK
    assert round_up(ERASE_BLOCK) == ERASE_BLOCK
    assert round_up(ERASE_BLOCK + 1) == 2 * ERASE_BLOCK
    # The kernel image from a real session: 1908952 bytes padded to 0x1E0000,
    # which is exactly what HiBurn wrote.
    assert round_up(1908952) == 0x1E0000
    assert round_up(5689344) == 0x570000


def test_builtin_layout_tiles_the_whole_chip():
    layout = get_layout("mjsxj02hl-16m")
    ordered = sorted(layout.partitions, key=lambda p: p.offset)
    assert ordered[0].offset == 0
    for previous, current in zip(ordered, ordered[1:], strict=False):
        assert previous.end == current.offset, "layout must leave no gaps"
    assert ordered[-1].end == layout.flash_size


def test_overlapping_partitions_are_rejected():
    with pytest.raises(LayoutError, match="overlaps"):
        FlashLayout(
            name="bad",
            flash_size=0x100000,
            partitions=(
                Partition("a", 0x0, 0x20000),
                Partition("b", 0x10000, 0x10000),
            ),
        )


def test_partition_past_the_end_of_flash_is_rejected():
    with pytest.raises(LayoutError, match="past the"):
        FlashLayout(
            name="bad",
            flash_size=0x100000,
            partitions=(Partition("a", 0xF0000, 0x20000),),
        )


def test_zero_sized_partition_is_rejected():
    with pytest.raises(LayoutError, match="non-positive"):
        FlashLayout(name="bad", flash_size=0x100000, partitions=(Partition("a", 0, 0),))


def test_check_fits_accounts_for_block_padding():
    layout = get_layout("mjsxj02hl-16m")
    layout.check_fits("env", 65536)
    # One byte over the partition still needs a whole extra erase block.
    with pytest.raises(LayoutError, match="partition holds only"):
        layout.check_fits("env", 65537)


def test_check_fits_rejects_an_oversized_rootfs():
    layout = get_layout("mjsxj02hl-16m")
    with pytest.raises(LayoutError):
        layout.check_fits("rootfs", 11 * 1024 * 1024)


def test_get_unknown_partition():
    with pytest.raises(LayoutError, match="no partition named"):
        get_layout("mjsxj02hl-16m").get("nope")


def test_unknown_layout_lists_what_exists():
    with pytest.raises(LayoutError, match="built in"):
        get_layout("does-not-exist")


def test_json_round_trip():
    original = get_layout("mjsxj02hl-16m")
    restored = FlashLayout.from_json(original.to_json())
    assert restored.partitions == original.partitions
    assert restored.flash_size == original.flash_size
    assert restored.staging_address == original.staging_address


def test_every_builtin_layout_validates():
    for name, layout in BUILTIN_LAYOUTS.items():
        layout.validate()
        assert layout.name == name
