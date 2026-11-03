/* Legacy layout contract shared by the image builder, loader, kernel and saver.
 * This describes an existing format, not this phone's partition geometry.
 * A new layout requires a distinct version and explicit migration/rejection.
 */
#ifndef THYME_STORAGE_LAYOUT_H
#define THYME_STORAGE_LAYOUT_H
#include <stdint.h>
#define THYME_LAYOUT_VERSION 1u
#define THYME_STORAGE_BLOCK_SIZE 4096ULL
#define THYME_STORE_FIRST_BLOCK 611200ULL
#define THYME_STORE_SLOT_BYTES 536870912ULL
#define THYME_STORE_SLOTS 2u
#define THYME_STORE_AT (THYME_STORE_FIRST_BLOCK * THYME_STORAGE_BLOCK_SIZE)
#define THYME_STORE_END (THYME_STORE_AT + THYME_STORE_SLOTS * THYME_STORE_SLOT_BYTES)
#define THYME_LAYOUT_V2_VERSION 2u
#define THYME_BLOCK_PROTOCOL_VERSION 4u
static inline int
thyme_block_protocol_valid(uint32_t version, uint32_t bytes, uint32_t expected_bytes)
{
	return version == THYME_BLOCK_PROTOCOL_VERSION && bytes == expected_bytes;
}

/* Serialized at header bytes64..95 in v2. All fields are little endian on
 * this target. The v2 digest must cover header bytes0..95 before image bytes.
 * It must never be interpreted as permission to replace an existing v1 store. */
struct thyme_store_layout {
	uint64_t store_at;
	uint64_t slot_bytes;
	uint32_t slots;
	uint32_t block_bytes;
	uint64_t flags;
};
_Static_assert(sizeof(struct thyme_store_layout) == 32, "v2 storage descriptor ABI");

static inline int
thyme_store_layout_valid(const struct thyme_store_layout *layout,
    uint64_t image_bytes, uint64_t partition_bytes)
{
	if (layout->flags != 0 || layout->slots != THYME_STORE_SLOTS ||
	    layout->block_bytes != THYME_STORAGE_BLOCK_SIZE ||
	    layout->slot_bytes < 2 * THYME_STORAGE_BLOCK_SIZE ||
	    layout->slot_bytes % THYME_STORAGE_BLOCK_SIZE != 0 ||
	    layout->store_at % THYME_STORAGE_BLOCK_SIZE != 0 ||
	    image_bytes < THYME_STORAGE_BLOCK_SIZE || image_bytes > layout->store_at ||
	    partition_bytes >= (1ULL << 63) || layout->store_at > partition_bytes) {
		return 0;
	}
	/* Division after subtraction avoids overflow in slots * slot_bytes. */
	return layout->slot_bytes <= (partition_bytes - layout->store_at) / layout->slots;
}

/* Immutable per-run descriptor copied through mailbox v4 and a read-only sysctl. */
struct thyme_store_info {
	uint32_t version;
	uint32_t reserved;
	uint64_t image_bytes;
	uint64_t partition_bytes;
	struct thyme_store_layout layout;
};
_Static_assert(sizeof(struct thyme_store_info) == 56, "runtime storage descriptor ABI");

static inline int
thyme_store_info_valid(const struct thyme_store_info *info)
{
	if (info->reserved != 0 ||
	    !thyme_store_layout_valid(&info->layout, info->image_bytes, info->partition_bytes)) {
		return 0;
	}
	if (info->version == THYME_LAYOUT_VERSION) {
		return info->layout.store_at == THYME_STORE_AT &&
		       info->layout.slot_bytes == THYME_STORE_SLOT_BYTES;
	}
	return info->version == THYME_LAYOUT_V2_VERSION;
}

static inline int
thyme_store_write_valid(const struct thyme_store_info *info, uint64_t offset, uint64_t bytes)
{
	if (!thyme_store_info_valid(info)) {
		return 0;
	}
	uint64_t end = info->layout.store_at + info->layout.slots * info->layout.slot_bytes;
	return bytes != 0 && offset % info->layout.block_bytes == 0 &&
	       bytes % info->layout.block_bytes == 0 && offset >= info->layout.store_at &&
	       offset <= end && bytes <= end - offset;
}
#endif
