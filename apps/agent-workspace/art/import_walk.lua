local root='/Users/ctavolazzi/Code/_experiments/SimpleAgentOS/apps/agent-workspace/'
for _,direction in ipairs({'south','east','north','west'}) do
 local first=app.open(root..'art/walk-frames/'..direction..'/0.png')
 local walk=Sprite(first.width,first.height,ColorMode.RGB)
 walk.layers[1].name='PixelLab Fieldwork walk '..direction
 for index=0,3 do
  local source=app.open(root..'art/walk-frames/'..direction..'/'..index..'.png')
  if index>0 then walk:newEmptyFrame() end
  walk:newCel(walk.layers[1],walk.frames[index+1],Image(source.cels[1].image))
  walk.frames[index+1].duration=0.13
 end
 local tag=walk:newTag(walk.frames[1],walk.frames[4]);tag.name='walk-'..direction
 walk:saveAs(root..'art/researcher-walk-'..direction..'.aseprite')
 app.activeSprite=walk
 app.command.ExportSpriteSheet{ui=false,type=SpriteSheetType.HORIZONTAL,textureFilename=root..'web/assets/researcher-walk-'..direction..'.png',dataFilename=root..'art/researcher-walk-'..direction..'.json',dataFormat=SpriteSheetDataFormat.JSON_ARRAY}
end
print('Saved four editable walk cycles and runtime sheets.')
