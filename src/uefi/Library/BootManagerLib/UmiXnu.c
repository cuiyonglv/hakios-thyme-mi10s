/* SPDX-License-Identifier: APSL-2.0
 * Adapted from azzlantern/hackios-thyme be4e3edb18c3215c6fd958882f8caae68dadfc2b.
 * RAM-only executable handoff; no filesystem or partition access.
 */
#include <Uefi.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include "UmiXnu.h"
#include "PayloadView.h"

#define THYME_PAYLOAD_BASE 0xF0000000ULL
#define THYME_PAYLOAD_SIZE 0x04000000ULL

VOID
UmiStartPayloadLoader (VOID)
{
  THYME_PAYLOAD_VIEW View;
  EFI_HANDLE Image = NULL;
  EFI_STATUS Status;

  Print (L"HackiOS thyme UEFI: checking complete RAM loader container\r\n");
  if (!ThymeFindPayloadLoader ((CONST UINT8 *)(UINTN)THYME_PAYLOAD_BASE,
                              THYME_PAYLOAD_SIZE, &View)) {
    Print (L"No valid complete XNUP loader container\r\n");
    return;
  }
  Status = gBS->LoadImage (FALSE, gImageHandle, NULL, (VOID *)View.Loader,
                           View.LoaderSize, &Image);
  Print (L"Thyme LoadImage: %r\r\n", Status);
  if (!EFI_ERROR (Status)) {
    Status = gBS->StartImage (Image, NULL, NULL);
    Print (L"Thyme loader returned: %r\r\n", Status);
    gBS->UnloadImage (Image);
  }
}
