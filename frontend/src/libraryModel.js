export const emptyCatalog = () => ({ projects: [], categories: [], types: [] })
export function categoryPath(catalog, id) {
  const names = [], seen = new Set()
  while (id && !seen.has(id)) {
    seen.add(id)
    const c = catalog.categories.find(x => x.id === id)
    if (!c) break
    names.unshift(c.name); id = c.parent_id
  }
  return names.join(' / ')
}
export function typeLabel(catalog, type) {
  return [categoryPath(catalog, type.category_id), type.name].filter(Boolean).join(' / ')
}
export function membershipLabel(catalog, sp) {
  const project = catalog.projects.find(p => p.id === sp?.project_id)
  if (!project) return '待归类'
  const type = catalog.types.find(t => t.id === sp?.sprite_type_id && t.project_id === project.id)
  return project.name + ' / ' + (type ? typeLabel(catalog, type) : '未指定类型')
}
export function matchesScope(catalog, sp, projectId, scope = '') {
  const validProject = catalog.projects.some(p => p.id === sp.project_id)
  if (projectId === 'unassigned') return !validProject
  if (projectId !== '*' && sp.project_id !== projectId) return false
  if (!scope) return true
  const type = catalog.types.find(t => t.id === sp.sprite_type_id && t.project_id === sp.project_id)
  if (scope === 'untyped') return !type
  if (scope.startsWith('st_')) return type?.id === scope
  let cat = type?.category_id
  const seen = new Set()
  while (cat && !seen.has(cat)) {
    if (cat === scope) return true
    seen.add(cat); cat = catalog.categories.find(c => c.id === cat)?.parent_id
  }
  return false
}
// Use the complete inventory, so a name/tag filter never hides the recovery entry.
export function unassignedCount(catalog, sprites) {
  return sprites.filter(sp => matchesScope(catalog, sp, 'unassigned')).length
}
export function resolveProjectSelection(catalog, sprites, selected) {
  if (selected === 'unassigned') return unassignedCount(catalog, sprites) ? selected : '*'
  return selected === '*' || catalog.projects.some(p => p.id === selected) ? selected : '*'
}
export function catalogTree(catalog, projectId, parent = null, seen = new Set()) {
  const categories = catalog.categories.filter(c => c.project_id === projectId && (c.parent_id || null) === parent && !seen.has(c.id))
  return [
    ...categories.map(c => ({ ...c, kind: 'category', children: catalogTree(catalog, projectId, c.id, new Set([...seen, c.id])) })),
    ...catalog.types.filter(t => t.project_id === projectId && (t.category_id || null) === parent).map(t => ({ ...t, kind: 'type', children: [] })),
  ]
}
