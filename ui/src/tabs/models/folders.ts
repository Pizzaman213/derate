/** The Hugging Face cache folder a repository lives in, as `GET /api/storage`
 *  names it and `DELETE /api/storage/...` takes it: `models--` plus the repo
 *  with every `/` turned into `--`. The hub's own layout, mirrored here so the
 *  Serve tab can delete a variant's weights without a lookup. */
export function folderFor(repo: string): string {
  return 'models--' + repo.trim().replaceAll('/', '--')
}
