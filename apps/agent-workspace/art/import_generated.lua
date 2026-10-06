-- Preserve generated directions as editable frames. Source PNGs stay unchanged.
local root='/Users/ctavolazzi/Code/_experiments/SimpleAgentOS/apps/agent-workspace/'
local directions={'south','east','north','west'}
local sheet=Sprite(48,48,ColorMode.RGB)
sheet.layers[1].name='PixelLab researcher directional poses'
for index,direction in ipairs(directions) do
 local source=app.open(root..'web/assets/researcher-'..direction..'.png')
 if index>1 then sheet:newEmptyFrame() end
 sheet:newCel(sheet.layers[1],sheet.frames[index],Image(source.cels[1].image))
 local tag=sheet:newTag(sheet.frames[index],sheet.frames[index]);tag.name=direction
end
sheet:saveAs(root..'art/researcher.aseprite')
local tank=app.open(root..'web/assets/water-tank.png')
tank.layers[1].name='PixelLab rainwater tank'
tank:saveAs(root..'art/water-tank.aseprite')
print('Saved editable generated poses and tank.')
