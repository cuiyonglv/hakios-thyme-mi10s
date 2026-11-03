/* Legacy layout contract shared by the image builder, loader, kernel and saver.
 * This describes an existing format, not this phone's partition geometry.
 * A new layout requires a distinct version and explicit migration/rejection.
 */
#ifndef THYME_STORAGE_LAYOUT_H
#define THYME_STORAGE_LAYOUT_H
#define THYME_LAYOUT_VERSION 1u
#define THYME_STORAGE_BLOCK_SIZE 4096ULL
#define THYME_STORE_FIRST_BLOCK 611200ULL
#define THYME_STORE_SLOT_BYTES 536870912ULL
#define THYME_STORE_SLOTS 2u
#define THYME_STORE_AT (THYME_STORE_FIRST_BLOCK * THYME_STORAGE_BLOCK_SIZE)
#define THYME_STORE_END (THYME_STORE_AT + THYME_STORE_SLOTS * THYME_STORE_SLOT_BYTES)
#endif
