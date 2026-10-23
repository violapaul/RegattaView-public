//
//  LIneView2.swift
//  BoatStart
//
//  Created by Paul Viola on 11/27/22.
//

import SwiftUI

struct LineView2: View {

    @ObservedObject var boatState: BoatState
    @ObservedObject var timeManager: TimeManager
    @State private var isShowing = true

//    struct Geometry {
//        var boatSize: CGFloat = 0.0
////        var size: CGSize
////        var pipSize: CGFloat
////        var position: Mapping.vec
//    }
//
//
//    var geometry = Geometry()

    let smallFont = Font
            .system(size: 60)
            .monospacedDigit()
        
    let tinyFont = Font
            .system(size: 20)
    
    
    var body: some View {
        
//      let distanceAtStart = boatState.state.distanceToLine - boatState.state.speedToLine * timeManager.timeToStart
//      let timeToBurn = (boatState.state.timeToLine - timeManager.timeToStart)
        
        GeometryReader { metrics in
            let size = metrics.size
            let unit = min(size.width, size.height)
            let sideOffset = unit/5.0
            let boatScreenSize = unit / 3.0  // How big are the boats on the screen
            // let pipSize = unit/15
            let pipSize = 2 * unit/15
            let lineLength = boatState.lineLength()
            let metersToPixels = (size.width * 0.6) / lineLength
            let along = boatState.distanceAlongLine()
            let toLine = boatState.distanceToLine()
            let position = Vector(0.8 * size.width - metersToPixels * along, metersToPixels * toLine)
            let timeToLine = String(format: "%02d", max(0, min(99,
                Int(round(boatState.state.timeToLine)))))
            // let distanceToLine = String(format: "%02d", max(0, min(99, Int(round(toLine)))))
            let distanceToLine = Ux.pf(toLine/Globals.boatLength, 1)
            ZStack {
                // ************************************************************
                // Flash black briefly on press.
                if isShowing {
                    Rectangle()
                        .onAppear {
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                                isShowing = false
                            }
                        }
                }
                //**************************************************************
                // BOAT: Make a big area on the boat side where you can tap
                HStack {
                    Rectangle()
                        .stroke(.red)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.pingBoatEdge()
                            isShowing = true
                            Alert.buttonPress()
                            print("boat edge tapped")
                        }
                }
                .frame(width: sideOffset, height: size.height)
                .position(x: size.width - sideOffset/2, y: size.height/2)
                //**************************************************************
                // PIN: Make a big area on the pin side where you can tap
                HStack {
                    Rectangle()
                        .stroke(.red)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.pingPinEdge()
                            isShowing = true
                            Alert.buttonPress()
                            print("pin edge tapped")
                        }
                }
                .frame(width: sideOffset, height: size.height)
                .position(x: sideOffset/2, y: size.height/2)
                //**************************************************************
                // Draw the boat
                HStack {
                    BoatShape()
                        .fill(.blue)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.pingBoat()
                            isShowing = true
                            Alert.buttonPress()
                            print("boat tapped")
                        }
                }
                .frame(width: boatScreenSize, height: boatScreenSize)
                .position(x: 0.8 * size.width, y: boatScreenSize/2)
                //**************************************************************
                // Draw the pin
                HStack {
                    Circle()
                        .fill(.blue)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.pingPin()
                            isShowing = true
                            Alert.buttonPress()
                            print("pin tapped")
                        }
                }
                .frame(width: boatScreenSize, height: boatScreenSize)
                .position(x: 0.2 * size.width, y: boatScreenSize/2)
                // *************************************************************
                // Draw the background and barge lines
                Canvas { context, size in
                    var offset = sideOffset/2.0
                    var color = Color.red
                    let bargeRect = CGRect(x: 0, y: offset, width: 2 * size.width, height: 1.0 * size.width)
                    //**************************************************************
                    // Draw a set of "boat length lines".  Helps with visualization.
                    while offset < size.height {
                        context.stroke(
                            MidLine()
                                .stroke(style: StrokeStyle(lineWidth: 1, dash: [10, 20]))
                                .path(in: CGRect(x: 0, y: offset, width: size.width, height: 1)),
                            with: .color(color))
                        offset = offset + metersToPixels * Globals.boatLength
                        color = Color.blue
                    }
                    //**************************************************************
                    // Draw the barge lines at the pin
                    context.stroke(
                        BargeLine()
                            .stroke(style: StrokeStyle(lineWidth: 1, dash: [10, 10]))
                            .path(in: bargeRect.offsetBy(dx: size.width * (-1.0 + 0.2), dy: 0)),
                        with: .color(Color.green))
                    // Draw the barge lines at the boat
                    context.stroke(
                        BargeLine()
                            .stroke(style: StrokeStyle(lineWidth: 1, dash: [10, 10]))
                            .path(in: bargeRect.offsetBy(dx: size.width * (-1.0 + 0.8), dy: 0)),
                        with: .color(Color.green))

                }
                // .contentShape(Rectangle())
                .allowsHitTesting(false)
                .opacity(0.8)
                //**************************************************************
                // Show some info at TOP
                VStack {
                    HStack {
                        Spacer()
                        Text(Ux.pf(boatState.linePerpAngle(), 0))
                        Spacer()
                    }
                    Spacer()
                }
                //**************************************************************
                // Show some info at the bottom.
                VStack {
                    Spacer()
                    HStack {
                        Spacer()
                        Text(timeManager.timeString)
                        Spacer()
                        Text(timeToLine)
                        Text("TTL").font(tinyFont)
                        Spacer()
                        Text(distanceToLine)
                        Text("BL").font(tinyFont)
                        Spacer()
                    }
                }
                //**************************************************************
                // Draw the boat
                HStack {
                    
                    BoatShape()
                        .fill(.red)
                        .contentShape(Rectangle())
                        .rotationEffect(Angle(degrees: boatState.state.hdg))
                }
                .frame(width: pipSize, height: pipSize)
                .position(x: position.x, y: position.y + sideOffset/2.0)

            }
        }
        .ignoresSafeArea(.all)
        .font(smallFont)
    }
}

struct LineView2_Previews: PreviewProvider {
    static var previews: some View {
        LineView2(boatState: BoatState(), timeManager: TimeManager())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
