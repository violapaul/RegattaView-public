//
//  VelocitekView.swift
//  BoatStart
//
//  Created by Paul Viola on 1/3/23.
//

import SwiftUI

func squash(val: Double, maxRange: Double) -> Double {
    if val > 0 {
        return sqrt(val) / sqrt(maxRange)
    } else {
        return -1.0 * sqrt(abs(val)) / sqrt(maxRange)
    }
}

struct LineInfoView: View {
    // var onTap: () -> Void
    var width: CGFloat
    var underOver: CGFloat
    
    @GestureState private var isTapped = false
    
    var body: some View {
        
        GeometryReader { metrics in
            let size = metrics.size
            let boxHeight = size.height * CGFloat(min(abs(underOver), 1.0) / 2.0)
            ZStack {
                if underOver < 0.0 {
                    Rectangle()
                        .fill(.red)
                        .frame(width: size.width, height: boxHeight)
                        .position(x: size.width/2, y: size.height/2 - boxHeight/2)
                } else {
                    Rectangle()
                        .fill(.green)
                        .frame(width: size.width, height: boxHeight)
                        .position(x: size.width/2, y: size.height/2 + boxHeight/2)
                }
                Rectangle()
                    .strokeBorder(.red, lineWidth: 3)
                    .contentShape(Rectangle())
                // .gesture(makeTap("boat edge tapped"))
            }
        }
        .frame(width: width)
    }
}

struct VelocitekView: View {

    @ObservedObject var timeManager: TimeManager
    @ObservedObject var boatState: BoatState
    
    let clockFont = Font
        .system(size: 190)
        .monospacedDigit()
    
    let mediumFont = Font
        .system(size: 90)
        .monospaced()
    
    let tinyFont = Font
        .system(size: 30)
        .monospacedDigit()
    
    var body: some View {

        let timeToLine = String(format: "%02d", max(0, min(99,
            Int(round(boatState.state.timeToLine)))))
        let distanceToLine = Ux.pf(min(99.0, boatState.state.distanceToLine / Globals.boatLength), 1)
        let distanceAtStart = boatState.state.distanceToLine - boatState.state.speedToLine * timeManager.timeToStart
        let timeToBurn = (boatState.state.timeToLine - timeManager.timeToStart)
//        let timeToBurnString = String(format: "%02d", max(-99, min(99,
//            Int(round(timeToBurn)))))

        GeometryReader { metrics in
            let size = metrics.size
            let rectWidth = size.width/10
            HStack (spacing: 0.0) {
                ZStack{
                    LineInfoView(width: rectWidth, underOver: squash(val: timeToBurn, maxRange: 60))
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.pingPin()
                            print("pin edge tapped")
                        }
                    Text("BURN")
                }
                VStack (alignment: .trailing, spacing: 0.0) {
                    Text(timeManager.timeString)
                        .frame(height: 190)
                    Rectangle().fill(.black).frame(height:10)
                    HStack(alignment: .lastTextBaseline) {
                        Text(timeToLine)
                        Text("s").font(tinyFont)
                        Spacer()
                        Text(distanceToLine)
                        Text("BL").font(tinyFont)
                    }
                    .frame(height: 190)
                }
                .frame(
                    maxWidth: .infinity,
                    maxHeight: .infinity
                )
                .font(clockFont)
                LineInfoView(width: rectWidth, underOver: distanceAtStart / 3.0)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        boatState.pingBoat()
                        print("boat edge tapped")
                    }
            }
        }
        .padding(-10)
        .ignoresSafeArea()
    }
}

struct VelocitekView_Previews: PreviewProvider {
    static var previews: some View {
        VelocitekView(timeManager: TimeManager(), boatState: BoatState())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
