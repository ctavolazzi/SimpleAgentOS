-- Editable production sheet: four crop states and four simulation props.
local root = '/Users/ctavolazzi/Code/_experiments/SimpleAgentOS/apps/agent-workspace/'
local s = Sprite(128,64,ColorMode.RGB)
s.layers[1].name = 'Garden states and workshop supplies'
local img=s.cels[1].image
local function color(hex)
 return Color{r=tonumber(hex:sub(1,2),16),g=tonumber(hex:sub(3,4),16),b=tonumber(hex:sub(5,6),16),a=255}
end
local function rect(x,y,w,h,c)
 for yy=y,y+h-1 do for xx=x,x+w-1 do img:drawPixel(xx,yy,color(c)) end end
end
for stage=0,3 do
 local x=stage*32
 rect(x+3,23,26,6,'604c39');rect(x+3,22,26,2,'a27b52');rect(x+5,27,22,2,'493f32')
 for j=0,2 do
  local xx=x+7+j*8
  if stage==0 then
   rect(xx,19,2,5,'718950');rect(xx-2,18,3,2,'90a55f');rect(xx+1,16,3,2,'adc779')
  elseif stage==3 then
   rect(xx,15,2,9,'9a8547');rect(xx-3,17,4,2,'b5a058');rect(xx+1,19,4,2,'827341')
  else
   rect(xx,12,2,12,'436541');rect(xx-3,15,4,3,'658c4b');rect(xx+1,12,4,3,'86ab5c');rect(xx-2,10,3,3,'9ab96b')
   if stage==2 then rect(xx-2,17,5,5,'ce7644');rect(xx-1,17,2,2,'efb663') end
  end
 end
 if stage==1 then for j=0,4 do rect(x+5+j*5,25,2,1,'739e9f') end end
end
-- toolbox
rect(5,45,23,15,'764c3b');rect(5,45,23,4,'ba7751');rect(7,58,19,2,'4e4236');rect(12,40,10,2,'c5a97b');rect(12,42,2,3,'6b5948');rect(20,42,2,3,'6b5948');rect(15,49,4,5,'d7b461')
-- stacked timber
for j=0,2 do rect(36+j*2,44+j*5,22,4,'956443');rect(36+j*2,44+j*5,22,1,'d1a36d');rect(37+j*2,45+j*5,2,2,'edc48e') end
-- harvested basket
rect(70,47,23,12,'ad8050');rect(72,58,19,2,'685339');rect(69,46,25,3,'dbb376')
for j=0,2 do rect(73+j*6,40,5,7,'d88a4a');rect(74+j*6,38,2,3,'6a8f4a');rect(74+j*6,41,2,2,'f0c26a') end
-- raindrops and cloud
rect(102,42,22,8,'adc6c1');rect(107,38,13,12,'d9e3d0');rect(100,46,26,5,'a6c4be')
for j=0,3 do rect(103+j*6,54,2,5,'6caaa9');rect(102+j*6,57,2,2,'8dc2bc') end
s:saveAs(root..'art/fieldwork-props.aseprite')
s:saveCopyAs(root..'web/assets/fieldwork-props.png')
-- Animated fountain sheet, four distinct frames, editable source timeline.
local water=Sprite(32,32,ColorMode.RGB)
water.layers[1].name='Pump flow'
for frame=1,4 do
 if frame>1 then water:newEmptyFrame() end
 local im=Image(32,32,ColorMode.RGB)
 local function r(x,y,w,h,c)
  for yy=y,y+h-1 do for xx=x,x+w-1 do im:drawPixel(xx,yy,color(c)) end end
 end
 r(5,22,24,6,'586e66');r(7,22,20,3,'9ab9ad');r(10,7,6,16,'81998a');r(9,5,9,4,'b4c4a8');r(15,8,10,4,'81998a');r(23,10,3,6,'b4c4a8');r(7,6,2,7,'635947')
 for y=15,23 do if (y+frame)%4<3 then r(24,y,2,1,'86ccc5') end end
 r(18+frame,24,3,1,'d3e6ce')
 water:newCel(water.layers[1],water.frames[frame],im)
 water.frames[frame].duration=0.18
end
water:saveAs(root..'art/pump-flow.aseprite')
app.command.ExportSpriteSheet{ui=false,type=SpriteSheetType.HORIZONTAL,textureFilename=root..'web/assets/pump-flow.png',dataFilename=root..'art/pump-flow.json',dataFormat=SpriteSheetDataFormat.JSON_ARRAY}
print('Saved editable prop sheet and four-frame pump animation.')
