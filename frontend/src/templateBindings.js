import api from './api'

export async function roleFirstFrameChoices(spriteId) {
  const [sprite, library] = await Promise.all([api.sprite(spriteId), api.ffsets()])
  const binding = sprite.template_binding || {}
  const sets = binding.group
    ? library.sets.filter(s => s.id === binding.first_frame_set_id && (s.group || '').trim() === binding.group)
    : library.sets
  // Legacy manual selection is permitted, but never silently choose the first set.
  return { sets, selected: binding.group && sets.length ? binding.first_frame_set_id : '', binding }
}
