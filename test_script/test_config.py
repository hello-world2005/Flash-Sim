"""Tests for configuration classes."""

import pytest
from flash_sim.config import TimingConfig, ParallelConfig, FlashConfig, FlashGeometry, FlashAddress, FlashTechnology


class TestTimingConfig:
    """Tests for TimingConfig."""

    def test_default_values(self):
        """Default timing values match the simulator baseline."""
        config = TimingConfig()
        assert config.technology == FlashTechnology.SLC
        assert config.t_r_lsb == 5_000
        assert config.t_r_csb == 100_000  # default for MLC/TLC
        assert config.t_r_msb == 150_000  # default for TLC
        assert config.t_prog_lsb == 250_000
        assert config.t_prog_csb == 1_000_000 # default for MLC/TLC
        assert config.t_prog_msb == 1_500_000 # default for TLC
        assert config.t_bers == 10_000_000

    def test_custom_values(self):
        """Custom timing values are accepted."""
        config = TimingConfig(
            t_r_lsb=100_000,
            t_r_msb=200_000,
            t_prog_lsb=500_000,
            t_prog_msb=1_000_000,
            t_bers=2_000_000
        )
        assert config.t_r_lsb == 100_000
        assert config.t_r_msb == 200_000
        assert config.t_prog_lsb == 500_000
        assert config.t_prog_msb == 1_000_000
        assert config.t_bers == 2_000_000

    def test_invalid_t_r_lsb_raises_error(self):
        """Zero or negative t_r_lsb raises ValueError."""
        with pytest.raises(ValueError, match="t_r_lsb must be positive"):
            TimingConfig(t_r_lsb=0)
        with pytest.raises(ValueError, match="t_r_lsb must be positive"):
            TimingConfig(t_r_lsb=-1)

    def test_invalid_t_prog_lsb_raises_error(self):
        """Zero or negative t_prog_lsb raises ValueError."""
        with pytest.raises(ValueError, match="t_prog_lsb must be positive"):
            TimingConfig(t_prog_lsb=0)

    def test_invalid_t_bers_raises_error(self):
        """Zero or negative t_bers raises ValueError."""
        with pytest.raises(ValueError, match="t_bers must be positive"):
            TimingConfig(t_bers=0)

    def test_mlc_requires_csb_timing(self):
        """MLC technology requires CSB timing values."""
        with pytest.raises(ValueError, match="t_r_csb must be positive"):
            TimingConfig(technology=FlashTechnology.MLC, t_r_csb=0)
        with pytest.raises(ValueError, match="t_r_csb must be positive"):
            TimingConfig(technology=FlashTechnology.MLC, t_r_csb=0)
        with pytest.raises(ValueError, match="t_r_msb must be positive"):
            TimingConfig(technology=FlashTechnology.MLC, t_r_msb=0)

    def test_tlc_requires_all_timing(self):
        """TLC technology requires LSB, CSB, and MSB timing values."""
        with pytest.raises(ValueError):
            TimingConfig(technology=FlashTechnology.TLC, t_r_csb=0)
        with pytest.raises(ValueError):
            TimingConfig(technology=FlashTechnology.TLC, t_r_msb=0)

    def test_get_read_latency(self):
        """get_read_latency returns correct latency by page type."""
        config = TimingConfig(
            technology=FlashTechnology.TLC,
            t_r_lsb=75_000,
            t_r_csb=100_000,
            t_r_msb=150_000
        )
        assert config.get_read_latency(0) == 75_000   # LSB
        assert config.get_read_latency(1) == 100_000  # CSB
        assert config.get_read_latency(2) == 150_000  # MSB

    def test_get_program_latency(self):
        """get_program_latency returns correct latency by page type."""
        config = TimingConfig(
            technology=FlashTechnology.TLC,
            t_prog_lsb=750_000,
            t_prog_csb=1_000_000,
            t_prog_msb=1_500_000
        )
        assert config.get_program_latency(0) == 750_000    # LSB
        assert config.get_program_latency(1) == 1_000_000  # CSB
        assert config.get_program_latency(2) == 1_500_000  # MSB

    def test_slc_uses_lsb_for_all_page_types(self):
        """SLC technology uses LSB latency for all page types."""
        config = TimingConfig(
            technology=FlashTechnology.SLC,
            t_r_lsb=75_000,
            t_r_csb=100_000,  # Ignored for SLC
            t_r_msb=150_000   # Ignored for SLC
        )
        assert config.get_read_latency(0) == 75_000
        assert config.get_read_latency(1) == 75_000  # Still LSB for SLC
        assert config.get_read_latency(2) == 75_000  # Still LSB for SLC


class TestParallelConfig:
    """Tests for ParallelConfig."""

    def test_default_values(self):
        """Default parallel values are reasonable."""
        config = ParallelConfig()
        assert config.max_parallel_wl == 64
        assert config.max_parallel_blocks == 8

    def test_custom_values(self):
        """Custom parallel values are accepted."""
        config = ParallelConfig(max_parallel_wl=128, max_parallel_blocks=16)
        assert config.max_parallel_wl == 128
        assert config.max_parallel_blocks == 16

    def test_invalid_wl_count_raises_error(self):
        """Zero or negative max_parallel_wl raises ValueError."""
        with pytest.raises(ValueError, match="max_parallel_wl must be positive"):
            ParallelConfig(max_parallel_wl=0)

    def test_invalid_block_count_raises_error(self):
        """Zero or negative max_parallel_blocks raises ValueError."""
        with pytest.raises(ValueError, match="max_parallel_blocks must be positive"):
            ParallelConfig(max_parallel_blocks=0)


class TestFlashGeometry:
    """Tests for FlashGeometry class (3D NAND)."""

    def test_default_values(self):
        """Default geometry has expected values for 3D NAND."""
        geo = FlashGeometry()
        assert geo.layers_per_block == 128   # Layers per block
        assert geo.sub_blocks_per_block == 4  # Sub-blocks per layer
        assert geo.blocks_per_plane == 1024
        assert geo.planes_per_die == 4
        assert geo.dies == 4
        # Derived values
        assert geo.pages_per_block == 512     # 128 * 4 = 512 (total pages per block)
        assert geo.pages_per_layer == 4       # sub_blocks_per_block = pages per layer

    def test_default_cim_geometry_and_payload_widths(self):
        geo = FlashGeometry()

        assert geo.wl_per_string == 128
        assert geo.bl_per_plane == 262_144
        assert geo.search_input_bits_per_wl == 1
        assert geo.search_match_bits_per_bl == 1
        assert geo.compute_input_bits_per_sl == 8
        assert geo.compute_accumulator_bits == 8

    @pytest.mark.parametrize(
        "field",
        [
            "wl_per_string",
            "bl_per_plane",
            "search_input_bits_per_wl",
            "search_match_bits_per_bl",
            "compute_input_bits_per_sl",
            "compute_accumulator_bits",
        ],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_cim_geometry_and_payload_widths_must_be_positive(self, field, value):
        with pytest.raises(ValueError, match=field):
            FlashGeometry(**{field: value})

    def test_calculated_properties(self):
        """Calculated properties are correct."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=256,
            planes_per_die=2,
            dies=2,
        )
        assert geo.pages_per_block == 1024     # 128 * 8
        assert geo.pages_per_layer == 8
        assert geo.pages_per_plane == 1024 * 256  # 262,144
        assert geo.pages_per_die == 262_144 * 2   # 524,288
        assert geo.blocks_per_die == 256 * 2      # 512
        assert geo.total_pages == 524_288 * 2     # 1,048,576
        assert geo.total_blocks == 512 * 2        # 1,024

    def test_page_to_address(self):
        """Page to address mapping works correctly with 3D geometry.

        pages_per_block = layers * sub_blocks = 128 * 8 = 1024
        pages_per_layer = sub_blocks = 8
        Each sub-block has exactly 1 page per layer.
        """
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=1
        )

        # Page 0 -> Die 0, Plane 0, Block 0, Layer 0, SubBlock 0, Page 0
        addr = geo.page_to_address(0)
        assert addr == FlashAddress(die=0, plane=0, block=0, layer=0, sub_block=0, page=0)

        # Page 7 -> Layer 0, SubBlock 7 (last sub-block in layer 0)
        addr = geo.page_to_address(7)
        assert addr.layer == 0
        assert addr.sub_block == 7
        assert addr.page == 0

        # Page 8 -> Layer 1, SubBlock 0
        addr = geo.page_to_address(8)
        assert addr.layer == 1
        assert addr.sub_block == 0
        assert addr.page == 0

        # Page 1023 -> Layer 127, SubBlock 7 (last page in block)
        addr = geo.page_to_address(1023)
        assert addr.layer == 127
        assert addr.sub_block == 7
        assert addr.page == 0

        # Page 1024 -> Block 1, Layer 0, SubBlock 0
        addr = geo.page_to_address(1024)
        assert addr.block == 1
        assert addr.layer == 0
        assert addr.sub_block == 0

        # Last page of first plane (1024 * 1024 - 1)
        last_page_plane = geo.pages_per_plane - 1
        addr = geo.page_to_address(last_page_plane)
        assert addr.die == 0
        assert addr.plane == 0
        assert addr.block == 1023
        assert addr.layer == 127
        assert addr.sub_block == 7
        assert addr.page == 0

    def test_page_to_address_out_of_range(self):
        """Out of range page raises error."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=1
        )
        with pytest.raises(ValueError, match="out of range"):
            geo.page_to_address(-1)
        with pytest.raises(ValueError, match="out of range"):
            # Beyond total pages (1024 * 1024 * 2 = 2,097,152)
            geo.page_to_address(geo.total_pages)

    def test_block_to_address(self):
        """Block to address mapping works correctly."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=1
        )

        # Block 0 -> Die 0, Plane 0, Block 0, Layer 0, SubBlock 0
        addr = geo.block_to_address(0)
        assert addr == FlashAddress(die=0, plane=0, block=0, layer=0, sub_block=0, page=-1)

        # Block 1023 -> Last block in plane 0
        addr = geo.block_to_address(1023)
        assert addr == FlashAddress(die=0, plane=0, block=1023, layer=0, sub_block=0, page=-1)

        # Block 1024 -> First block in plane 1
        addr = geo.block_to_address(1024)
        assert addr == FlashAddress(die=0, plane=1, block=0, layer=0, sub_block=0, page=-1)

    def test_address_to_page_and_back(self):
        """Round-trip page conversion works."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=2
        )

        for page in [0, 100, 1000, 100000, geo.total_pages - 1]:
            addr = geo.page_to_address(page)
            back = geo.address_to_page(addr)
            assert back == page, f"Round-trip failed for page {page}"

    def test_address_to_block_and_back(self):
        """Round-trip block conversion works."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=2
        )

        for block in [0, 100, 1000, geo.total_blocks - 1]:
            addr = geo.block_to_address(block)
            back = geo.address_to_block(addr)
            assert back == block, f"Round-trip failed for block {block}"


class TestFlashAddress:
    """Tests for FlashAddress class."""

    def test_default_values(self):
        """Default address has all zeros."""
        addr = FlashAddress()
        assert addr.die == 0
        assert addr.plane == 0
        assert addr.block == 0
        assert addr.layer == 0
        assert addr.sub_block == 0
        assert addr.page == -1

    def test_page_address(self):
        """Page address includes page index."""
        addr = FlashAddress(die=1, plane=0, block=100, page=50)
        assert addr.page == 50
        assert addr.layer == 0
        assert addr.sub_block == 0

    def test_block_address(self):
        """Block address has page=-1."""
        addr = FlashAddress(die=0, plane=1, block=500, page=-1)
        assert addr.page == -1
        assert addr.layer == 0
        assert addr.sub_block == 0

    def test_layer_address(self):
        """Address with explicit layer."""
        addr = FlashAddress(die=0, plane=0, block=10, layer=5, sub_block=2, page=100)
        assert addr.layer == 5
        assert addr.sub_block == 2
        assert addr.page == 100

    def test_str_representation(self):
        """String representation is correct."""
        addr = FlashAddress(die=0, plane=1, block=100, page=50)
        assert "Die:0" in str(addr)
        assert "Plane:1" in str(addr)
        assert "Block:100" in str(addr)
        assert "Layer:0" in str(addr)
        assert "SubBlock:0" in str(addr)
        assert "Page:50" in str(addr)

        block_addr = FlashAddress(die=0, plane=1, block=100, page=-1)
        assert "Die:0 Plane:1 Block:100 Layer:0" in str(block_addr)

        addr_with_layer = FlashAddress(die=0, plane=1, block=100, layer=3, page=50)
        assert "Layer:3" in str(addr_with_layer)

        addr_with_subblock = FlashAddress(die=0, plane=0, block=10, layer=5, sub_block=2, page=15)
        assert "SubBlock:2" in str(addr_with_subblock)
        assert "Page:15" in str(addr_with_subblock)
        assert "Page:15" in str(addr_with_subblock)
        assert "Page:50" in str(addr_with_layer)


class TestFlashConfig:
    """Tests for FlashConfig."""

    def test_default_config(self):
        """Default config has expected values."""
        config = FlashConfig()
        assert isinstance(config.timing, TimingConfig)
        assert isinstance(config.parallel, ParallelConfig)
        assert isinstance(config.geometry, FlashGeometry)
        assert config.geometry.layers_per_block == 128
        assert config.geometry.sub_blocks_per_block == 4
        assert config.geometry.pages_per_block == 512  # 128 * 4
        assert config.blocks_per_plane == 1024
        assert config.planes_per_die == 4
        assert config.geometry.dies == 4

    def test_from_dict(self):
        """Config can be created from dictionary."""
        config_dict = {
            "timing": {"t_r_lsb": 100_000, "t_prog_lsb": 600_000},
            "parallel": {"max_parallel_wl": 32},
            "geometry": {"layers_per_block": 64, "sub_blocks_per_block": 4},
        }
        config = FlashConfig.from_dict(config_dict)
        assert config.timing.t_r_lsb == 100_000
        assert config.timing.t_prog_lsb == 600_000
        assert config.timing.t_bers == TimingConfig().t_bers
        assert config.parallel.max_parallel_wl == 32
        assert config.parallel.max_parallel_blocks == 8  # default
        assert config.geometry.layers_per_block == 64
        assert config.geometry.sub_blocks_per_block == 4
        assert config.geometry.pages_per_block == 256  # 64 * 4

    def test_runtime_cache_capacity_aliases(self):
        """Runtime cache capacity can be configured with compatibility aliases."""
        assert (
            FlashConfig.from_dict({"runtime": {"data_cache_capacity": 64 * 1024}})
            .runtime
            .data_cache_capacity
            == 64 * 1024
        )
        assert (
            FlashConfig.from_dict({"runtime": {"cache_capacity": 128 * 1024}})
            .runtime
            .data_cache_capacity
            == 128 * 1024
        )
        assert (
            FlashConfig.from_dict({"runtime": {"cache_cap": 32 * 1024}})
            .runtime
            .data_cache_capacity
            == 32 * 1024
        )

    def test_to_dict(self):
        """Config can be serialized to dictionary."""
        config = FlashConfig()
        config_dict = config.to_dict()
        assert config_dict["timing"]["t_r_lsb"] == config.timing.t_r_lsb
        assert config_dict["timing"]["technology"] == "slc"
        assert config_dict["parallel"]["max_parallel_wl"] == 64
        assert config_dict["geometry"]["layers_per_block"] == 128
        assert config_dict["geometry"]["sub_blocks_per_block"] == 4

    def test_from_dict_empty(self):
        """Empty dict produces default config."""
        config = FlashConfig.from_dict({})
        assert config == FlashConfig()

    def test_cim_geometry_fields_round_trip(self):
        geometry_values = {
            "wl_per_string": 96,
            "bl_per_plane": 131_072,
            "search_input_bits_per_wl": 2,
            "search_match_bits_per_bl": 3,
            "compute_input_bits_per_sl": 4,
            "compute_accumulator_bits": 12,
        }

        config = FlashConfig.from_dict({"geometry": geometry_values})

        assert {
            field: config.to_dict()["geometry"][field] for field in geometry_values
        } == geometry_values


class TestFlashGeometry3D:
    """Tests for 3D NAND geometry features with sub-blocks.

    In this model:
    - pages_per_block = layers * sub_blocks
    - pages_per_layer = sub_blocks
    - Each sub-block has exactly 1 page per layer (page is always 0)
    """

    def test_3d_default_structure(self):
        """3D geometry defaults for 128-layer NAND."""
        geo = FlashGeometry()
        assert geo.layers_per_block == 128
        assert geo.sub_blocks_per_block == 4
        assert geo.pages_per_block == 512    # 128 * 4
        assert geo.pages_per_layer == 4

    def test_3d_with_sub_blocks(self):
        """3D NAND with layers and sub-blocks."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8
        )
        assert geo.pages_per_block == 1024     # 128 * 8
        assert geo.pages_per_layer == 8
        assert geo.layers_per_plane == 128 * 1024  # 131,072
        assert geo.layers_per_die == geo.layers_per_plane * geo.planes_per_die

    def test_3d_calculated_pages(self):
        """3D geometry page calculations."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=1
        )
        assert geo.pages_per_block == 1024
        assert geo.pages_per_layer == 8
        assert geo.pages_per_plane == 1024 * 1024  # 1,048,576
        assert geo.pages_per_die == 1_048_576 * 2   # 2,097,152
        assert geo.total_pages == 2_097_152

    def test_3d_page_to_address(self):
        """Page to address with 3D geometry and sub-blocks.

        pages_per_block = layers * sub_blocks = 128 * 8 = 1024
        """
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8
        )

        # Page 0 -> Layer 0, SubBlock 0, Page 0
        addr = geo.page_to_address(0)
        assert addr.layer == 0
        assert addr.sub_block == 0
        assert addr.page == 0
        assert addr.block == 0

        # Page 7 -> Layer 0, SubBlock 7 (last sub-block in layer 0)
        addr = geo.page_to_address(7)
        assert addr.layer == 0
        assert addr.sub_block == 7
        assert addr.page == 0

        # Page 8 -> Layer 1, SubBlock 0
        addr = geo.page_to_address(8)
        assert addr.layer == 1
        assert addr.sub_block == 0
        assert addr.page == 0

        # Page 1023 -> Layer 127, SubBlock 7 (last page in block)
        addr = geo.page_to_address(1023)
        assert addr.layer == 127
        assert addr.sub_block == 7
        assert addr.page == 0

        # Page 1024 -> Block 1, Layer 0, SubBlock 0
        addr = geo.page_to_address(1024)
        assert addr.block == 1
        assert addr.layer == 0
        assert addr.sub_block == 0
        assert addr.page == 0

    def test_3d_address_to_page(self):
        """Address to page with 3D geometry and sub-blocks.

        page is always 0 since each sub-block has 1 page per layer.
        pages_per_layer = sub_blocks = 8
        pages_per_block = layers * sub_blocks = 128 * 8 = 1024
        """
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8
        )

        # Layer 0, SubBlock 0 -> Page 0
        addr = FlashAddress(die=0, plane=0, block=0, layer=0, sub_block=0, page=0)
        assert geo.address_to_page(addr) == 0

        # Layer 0, SubBlock 1 -> Page 1 (each sub-block has 1 page)
        addr = FlashAddress(die=0, plane=0, block=0, layer=0, sub_block=1, page=0)
        assert geo.address_to_page(addr) == 1

        # Layer 1, SubBlock 0 -> Page 8 (8 sub-blocks per layer)
        addr = FlashAddress(die=0, plane=0, block=0, layer=1, sub_block=0, page=0)
        assert geo.address_to_page(addr) == 8

        # Layer 5, SubBlock 1 -> Page (5 * 8 + 1) = 41
        addr = FlashAddress(die=0, plane=0, block=0, layer=5, sub_block=1, page=0)
        assert geo.address_to_page(addr) == 41

        # Layer 127, SubBlock 7 -> Page 1023 (last in block)
        addr = FlashAddress(die=0, plane=0, block=0, layer=127, sub_block=7, page=0)
        assert geo.address_to_page(addr) == 1023

    def test_3d_round_trip(self):
        """Round-trip conversion works for 3D addresses."""
        geo = FlashGeometry(
            layers_per_block=128,
            sub_blocks_per_block=8,
            blocks_per_plane=1024,
            planes_per_die=2,
            dies=1
        )

        # Test pages across different layers
        test_pages = [
            0,                           # First page
            7,                           # Last sub-block of layer 0
            8,                           # First sub-block of layer 1
            1023,                        # Last page of block 0
            1024,                        # First page of block 1
            2047,                        # Last page of plane 0
            geo.pages_per_die - 1,       # Last page of die 0
        ]
        for page in test_pages:
            addr = geo.page_to_address(page)
            back = geo.address_to_page(addr)
            assert back == page, f"Round-trip failed for page {page}"

        # Test specific 3D addresses
        addr = FlashAddress(die=0, plane=0, block=0, layer=0, sub_block=0, page=0)
        assert geo.address_to_page(addr) == 0

        addr = FlashAddress(die=0, plane=0, block=0, layer=5, sub_block=1, page=0)
        assert geo.address_to_page(addr) == 41

        addr = FlashAddress(die=0, plane=0, block=0, layer=127, sub_block=7, page=0)
        assert geo.address_to_page(addr) == 1023

    def test_invalid_layer_raises_error(self):
        """Invalid layer raises ValueError."""
        geo = FlashGeometry(layers_per_block=128, sub_blocks_per_block=8)

        addr = FlashAddress(die=0, plane=0, block=0, layer=128, sub_block=0, page=0)
        with pytest.raises(ValueError, match="Layer 128 out of range"):
            geo.address_to_page(addr)

    def test_invalid_sub_block_raises_error(self):
        """Invalid sub-block raises ValueError."""
        geo = FlashGeometry(layers_per_block=128, sub_blocks_per_block=8)

        addr = FlashAddress(die=0, plane=0, block=0, layer=0, sub_block=8, page=0)
        with pytest.raises(ValueError, match="Sub-block 8 out of range"):
            geo.address_to_page(addr)


class TestFlashAddress3D:
    """Tests for 3D FlashAddress features with sub-blocks."""

    def test_3d_address_creation(self):
        """3D address includes layer and sub_block fields."""
        addr = FlashAddress(die=0, plane=0, block=10, layer=5, sub_block=3, page=100)
        assert addr.die == 0
        assert addr.plane == 0
        assert addr.block == 10
        assert addr.layer == 5
        assert addr.sub_block == 3
        assert addr.page == 100

    def test_3d_default_layer_and_sub_block(self):
        """Default layer and sub_block are 0 for backward compatibility."""
        addr = FlashAddress(die=0, plane=0, block=10, page=100)
        assert addr.layer == 0
        assert addr.sub_block == 0

    def test_3d_block_address(self):
        """3D block address includes layer and sub_block."""
        addr = FlashAddress(die=0, plane=1, block=500, page=-1)
        assert addr.layer == 0  # Default layer
        assert addr.sub_block == 0  # Default sub_block
        assert addr.page == -1

    def test_3d_str_representation(self):
        """String representation includes layer and sub_block."""
        addr = FlashAddress(die=0, plane=1, block=100, layer=7, sub_block=2, page=50)
        assert "Die:0" in str(addr)
        assert "Plane:1" in str(addr)
        assert "Block:100" in str(addr)
        assert "Layer:7" in str(addr)
        assert "SubBlock:2" in str(addr)
        assert "Page:50" in str(addr)


class TestFlashConfig3D:
    """Tests for 3D configuration features with sub-blocks."""

    def test_3d_config_from_dict(self):
        """Config with layers_per_block and sub_blocks_per_block from dictionary."""
        config_dict = {
            "geometry": {
                "layers_per_block": 128,
                "sub_blocks_per_block": 8,
            }
        }
        config = FlashConfig.from_dict(config_dict)
        assert config.geometry.pages_per_block == 1024   # 128 * 8
        assert config.geometry.layers_per_block == 128
        assert config.geometry.sub_blocks_per_block == 8

    def test_3d_config_to_dict(self):
        """Config serialization includes sub_blocks_per_block."""
        config = FlashConfig()
        config.geometry.layers_per_block = 128
        config.geometry.sub_blocks_per_block = 8
        config_dict = config.to_dict()
        assert config_dict["geometry"]["layers_per_block"] == 128
        assert config_dict["geometry"]["sub_blocks_per_block"] == 8
        # pages_per_block is derived (128 * 8 = 1024), not stored in dict

    def test_3d_config_default(self):
        """Default config uses the documented 4/4/4 geometry."""
        config = FlashConfig()
        assert config.geometry.layers_per_block == 128
        assert config.geometry.sub_blocks_per_block == 4
        assert config.geometry.planes_per_die == 4
        assert config.geometry.dies == 4
        assert config.geometry.pages_per_block == 512  # 128 * 4
