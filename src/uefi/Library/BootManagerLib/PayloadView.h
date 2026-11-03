/* SPDX-License-Identifier: MIT
 * Bounded XNUP loader selection. UEFI types are supplied by the caller.
 */
#ifndef THYME_PAYLOAD_VIEW_H
#define THYME_PAYLOAD_VIEW_H

typedef struct {
  CONST UINT8 *Loader;
  UINTN LoaderSize;
} THYME_PAYLOAD_VIEW;

STATIC UINT32
ThymePayloadU32 (CONST UINT8 *Data)
{
  return (UINT32)Data[0] | ((UINT32)Data[1] << 8) |
         ((UINT32)Data[2] << 16) | ((UINT32)Data[3] << 24);
}

STATIC BOOLEAN
ThymeFindPayloadLoader (CONST UINT8 *Data, UINTN Available, THYME_PAYLOAD_VIEW *View)
{
  CONST UINT8 *Candidate = NULL;
  UINTN CandidateSize = 0;
  UINTN Position = 8;
  UINTN Remaining;
  UINTN Entries = 0;
  STATIC CONST UINT8 LoaderName[11] = "loader.efi";

  if (View == NULL) return FALSE;
  View->Loader = NULL;
  View->LoaderSize = 0;
  if (Data == NULL || Available < 8 || Available > 0x04000000 ||
      (UINTN)Data > ~(UINTN)0 - Available) return FALSE;
  if (ThymePayloadU32 (Data) != 0x50554e58) return FALSE;
  Remaining = ThymePayloadU32 (Data + 4);
  if (Remaining > Available - 8) return FALSE;

  while (Remaining >= 24) {
    CONST UINT8 *Entry = Data + Position;
    UINTN NameLength = 0;
    UINTN Index;
    UINT64 Padded;
    UINT32 Size;
    BOOLEAN IsLoader = TRUE;

    if (Entry[0] == 0) {
      /* The complete terminal area must be zero, including its size fields. */
      for (Index = 0; Index < Remaining; Index++) {
        if (Entry[Index] != 0) return FALSE;
      }
      if (Candidate == NULL) return FALSE;
      View->Loader = Candidate;
      View->LoaderSize = CandidateSize;
      return TRUE;
    }
    if (++Entries > 256) return FALSE;
    while (NameLength < 16 && Entry[NameLength] != 0) {
      UINT8 Ch = Entry[NameLength++];
      if (!((Ch >= 'a' && Ch <= 'z') || (Ch >= 'A' && Ch <= 'Z') ||
            (Ch >= '0' && Ch <= '9') || Ch == '.' || Ch == '_' || Ch == '-')) return FALSE;
    }
    if (NameLength == 16) return FALSE;
    for (Index = NameLength; Index < 16; Index++) {
      if (Entry[Index] != 0) return FALSE;
    }
    if (ThymePayloadU32 (Entry + 20) != 0) return FALSE;
    Size = ThymePayloadU32 (Entry + 16);
    Padded = ((UINT64)Size + 15) & ~(UINT64)15;
    if (Padded > Remaining - 24) return FALSE;
    for (Index = 0; Index < sizeof (LoaderName); Index++) {
      if (Entry[Index] != LoaderName[Index]) IsLoader = FALSE;
    }
    if (IsLoader) {
      if (Candidate != NULL || Size == 0) return FALSE;
      Candidate = Entry + 24;
      CandidateSize = Size;
    }
    Position += 24 + (UINTN)Padded;
    Remaining -= 24 + (UINTN)Padded;
  }
  /* Never publish a candidate from a truncated or unterminated container. */
  return FALSE;
}
#endif
