'''
@author: aj carattino
'''
import numpy as np
import sys
import os
import psutil
import time
import pyqtgraph as pg
import h5py

from multiprocessing import Process, Queue
from pyqtgraph.Qt import QtGui, QtCore
from pyqtgraph import GraphicsLayoutWidget
from PyQt4.Qt import QApplication

from datetime import datetime
from Model.camera import workerSaver
from View.Camera.cameraViewer import cameraViewer
from View.Camera.workerThread import workThread
from View.Camera.clearQueueThread import clearQueueThread
from View.Camera.configWidget import configWidget
from View.Camera.waterfallWidget import waterfallWidget

class cameraMain(QtGui.QMainWindow):
    """ Displays the camera.
    """
    def __init__(self,session,cam,parent=None):
        super(cameraMain,self).__init__()
        self._session = session
        self.camera = cam
        self._session.camera['camera'] = self.camera

        self.fileDir = self._session.saveDirectory
        self.fileName = self._session.filenamePhoto
        self.movieName = self._session.filenameVideo
        # Queue of images. multiprocessing takes care of handling the data in and out
        # and the sharing between parent and child processes.
        self.q = Queue(0)

        self.setWindowTitle('Camera Monitor')
        self.camWidget = cameraWidget()
        self.setCentralWidget(self.camWidget)

        # self.movieTimer = QtCore.QTimer()
        # self.connect(self.movieTimer,QtCore.SIGNAL("timeout()"),self.movieData)

        self.refreshTimer = QtCore.QTimer()
        self.connect(self.refreshTimer,QtCore.SIGNAL('timeout()'),self.updateGUI)

        self.refreshTimer.start(self._session.refreshTime)

        # self.connect(self.workerThread,QtCore.SIGNAL('Image'),self.getData)

        # Worker thread for clearing the queue.
        self.clearWorker = clearQueueThread(self.q)

        # Window for the camera viewer
        self.camViewer = cameraViewer(self._session,self.camera,parent=self)
        self.connect(self.camViewer,QtCore.SIGNAL('Stop_MainAcquisition'),self.stopMovie)
        self.connect(self,QtCore.SIGNAL('stopChildMovie'),self.camViewer.stopCamera)
        self.connect(self,QtCore.SIGNAL('CloseAll'),self.camViewer.closeViewer)

        self.config = configWidget(self._session)
        self.connect(self.config,QtCore.SIGNAL('updateConfig'),self.updateSession)
        self.connect(self,QtCore.SIGNAL('CloseAll'),self.config.close)
        self.setupActions()
        self.setupToolbar()
        self.setupMenubar()
        self.acquiring = False
        self.logMessage = []

        ### Initialize the camera and the camera related things ###
        self.maxSize = self.camera.maxSize
        self.camWidget.vline2.setValue(self.maxSize[0]-1)
        self.camWidget.hline2.setValue(self.maxSize[1]-1)
        self.camWidget.hline1.setBounds((1,self.maxSize[1]-1))
        self.camWidget.hline2.setBounds((1,self.maxSize[1]-1))
        self.camWidget.vline1.setBounds((1,self.maxSize[0]-1))
        self.camWidget.vline2.setBounds((1,self.maxSize[0]-1))
        self._session.ROIl = 1
        self._session.ROIr = self.maxSize[0]
        self._session.ROIu = self.maxSize[1]
        self._session.ROIb = 1

        self.lastBuffer = time.time()
        self.lastRefresh = time.time()

        self.tempImage = []
        self.saveRunning = False
        self.accumulateBuffer = False
        self.bufferTime = 0
        self.bufferTimes = []
        self.refreshTimes = []
        self.totalFrames = 0
        self.continousSaving = False
        self.showWaterfall = False


    def snap(self):
        """Function for acquiring a single frame from the camera. It is triggered by the user.
        It gets the data the GUI will be updated at a fixed framerate.
        """
        if self.acquiring: #If it is itself acquiring a message is displayed to the user warning him
            msgBox = QtGui.QMessageBox()
            msgBox.setIcon(QtGui.QMessageBox.Critical)
            msgBox.setText("You cant snap a photo while in free run")
            msgBox.setInformativeText("The program is already acquiring data")
            msgBox.setWindowTitle("Already acquiring")
            msgBox.setDetailedText("""When in free run, you can\'t trigger another acquisition. \n
                You should stop the free run acquisition and then snap a photo.""")
            msgBox.setStandardButtons(QtGui.QMessageBox.Ok)
            retval = msgBox.exec_()
            self.logMessage.append('<b>Error: </b>Tried to snap while in free run')
        else:
            self.workerThread = workThread(self._session,self.camera)
            self.connect(self.workerThread,QtCore.SIGNAL('Image'),self.getData)
            self.workerThread.origin = 'snap'
            self.workerThread.start()

            self.acquiring = True

    def saveImage(self):
        """Saves the image that is being displayed to the user.
        """
        if len(self.tempImage) >= 1:
            # Not overwrite the file
            i=1
            filename = '%s_%s.hdf5'%(self.fileName,i)
            while os.path.exists(os.path.join(self.fileDir,filename)):
                i += 1
                filename = '%s_%s.hdf5' %(self.fileDir,i)
            f = h5py.File(os.path.join(self.fileDir,filename), "w")
            dset = f.create_dataset('image', data=self.tempImage)

    def startMovie(self):
        if self.acquiring:
            self.stopMovie()
        else:
            self.emit(QtCore.SIGNAL('stopChildMovie'))
            self.logMessage.append('<b>Info: </b>Started free run movie')
            # Worker thread to acquire images. Specially useful for long exposure time images
            self.workerThread = workThread(self._session,self.camera)
            self.connect(self.workerThread,QtCore.SIGNAL('Image'),self.getData)
            self.workerThread.start()
            self.acquiring = True


    def stopMovie(self):
        if self.acquiring:
            self.workerThread.keep_acquiring = False
            self.acquiring = False
            self.logMessage.append('<b>Info: </b>Stopped free run movie')

    def movieData(self):
        """Function just to trigger and read the camera in the separate thread.
        """
        self.workerThread.start()

    def movieSave(self):
        """Saves the data accumulated in the queue continuously.
        """
        if not self.continousSaving:
            # Child process to save the data. It runs continuously until and exit flag
            # is passed through the Queue. (self.q.put('exit'))
            to_save = os.path.join(self.fileDir,self.movieName)
            metaData = {}
            metaData['User'] = self._session.user
            metaData['exposureTime'] = self._session.exposureTime
            self.p = Process(target=workerSaver,args=(to_save,metaData,self.q,))
            self.p.start()
            self.continousSaving = True
            self.logMessage.append('<b>Info:</b> Started the Continuous savings')
        else:
            self.logMessage.append('<b>WARNING</b>: Continuous savings already triggered')

    def movieSaveStop(self):
        """Stops the saving to disk. It will however flush the queue.
        """
        if self.continousSaving:
            self.q.put('Stop')
            #self.p.join()
            self.logMessage.append('<b>Info:</b> Stopped the Continuous savings')
            self.continousSaving = False

    def emptyQueue(self):
        """Clears the queue.
        """
        self.clearWorker.start()

    def startWaterfall(self):
        """Starts the waterfall. The waterfall can be accelerated if camera supports hardware binning in the appropriate direction. If not, has to be done via software but the acquisition time cannot be improved.
        IDEA: Fast waterfall should have separate window, since the acquisition of the full CCD will be stopped.
        """
        if not self.showWaterfall:
            self.watWidget = waterfallWidget()
            self.camWidget.layout.addWidget(self.watWidget)
            self.showWaterfall = True
            Sx,Sy = self.camera.getSize()
            print(Sx,Sy)
            self.watData = np.zeros((self._session.lengthWaterfall,Sx))
            self.watWidget.img.setImage(np.transpose(self.watData))
            self.logMessage.append('<b>Info:</b> Waterfall opened')
        else:
            self.closeWaterfall()

    def stopWaterfall(self):
        """Stops the acquisition of the waterfall.
        """
        pass

    def closeWaterfall(self):
        """Closes the waterfall widget.
        """
        if self.showWaterfall:
            self.watWidget.close()
            self.showWaterfall = False
            del self.watData
            self.logMessage.append('<b>Info:</b> Waterfall closed')

    def setROI(self):
        """Gets the ROI from the lines on the image. It also updates the GUI to accomodate the changes.
        """
        if not self.acquiring:
            y1 = np.int(self.camWidget.hline1.value())
            y2 = np.int(self.camWidget.hline2.value())
            x1 = np.int(self.camWidget.vline1.value())
            x2 = np.int(self.camWidget.vline2.value())
            X = np.sort((x1,x2))
            Y = np.sort((y1,y2))
            self._session.ROIl = X[0]
            self._session.ROIr = X[1]
            self._session.ROIu = Y[1]
            self._session.ROIb = Y[0]
            Nx,Ny = self.camera.setROI(X,Y)
            self.tempImage = np.zeros((Nx,Ny))
            self.camWidget.hline1.setValue(1)
            self.camWidget.hline2.setValue(Ny)
            self.camWidget.vline1.setValue(1)
            self.camWidget.vline2.setValue(Nx)
            if self.showWaterfall:
                self.watData = np.zeros((self._session.lengthWaterfall,Nx))
        else:
            self.logMessage.append('<b>Error: <b> Cannot change ROI while acquiring.')

    def clearROI(self):
        """Resets the roi to the full image.
        """
        if not self.acquiring:
            X = np.array((1,self.maxSize[0]))
            Y = np.array((1,self.maxSize[1]))
            Nx,Ny = self.camera.setROI(X,Y) # Is this correct?
            self.tempImage = np.zeros((self.maxSize[0],self.maxSize[1]))
            self.camWidget.hline1.setValue(1)
            self.camWidget.vline1.setValue(1)
            self.camWidget.vline2.setValue(self.maxSize[0])
            self.camWidget.hline2.setValue(self.maxSize[1])
            if self.showWaterfall:
                self.watData = np.zeros((self._session.lengthWaterfall,Nx))
        else:
            self.logMessage.append('<b>Error: <b> Cannot change ROI while acquiring.')

    def setupActions(self):
        """Setups the actions that the program will have. It is placed into a function
        to make it easier to reuse in other windows.
        """
        self.exitAction = QtGui.QAction(QtGui.QIcon('View/Icons/power-icon.png'), '&Exit', self)
        self.exitAction.setShortcut('Ctrl+Q')
        self.exitAction.setStatusTip('Exit application')
        self.exitAction.triggered.connect(self.exitSafe)

        self.saveAction = QtGui.QAction(QtGui.QIcon('View/Icons/floppy-icon.png'),'&Save image',self)
        self.saveAction.setShortcut('Ctrl+S')
        self.saveAction.setStatusTip('Save Image')
        self.saveAction.triggered.connect(self.saveImage)

        self.snapAction = QtGui.QAction(QtGui.QIcon('View/Icons/snap.png'),'S&nap photo',self)
        self.snapAction.setShortcut(QtCore.Qt.Key_F5)
        self.snapAction.setStatusTip('Snap Image')
        self.snapAction.triggered.connect(self.snap)

        self.movieAction = QtGui.QAction(QtGui.QIcon('View/Icons/video-icon.png'),'Start &movie',self)
        self.movieAction.setShortcut('Ctrl+R')
        self.movieAction.setStatusTip('Start Movie')
        self.movieAction.triggered.connect(self.startMovie)

        self.movieSaveStartAction = QtGui.QAction(QtGui.QIcon('View/Icons/Download-Database-icon.png'),'Continuous saves',self)
        self.movieSaveStartAction.setShortcut('Ctrl+M')
        self.movieSaveStartAction.setStatusTip('Continuous save to disk')
        self.movieSaveStartAction.triggered.connect(self.movieSave)

        self.movieSaveStopAction = QtGui.QAction(QtGui.QIcon('View/Icons/Delete-Database-icon.png'),'Stop continuous saves',self)
        self.movieSaveStopAction.setShortcut('Ctrl+N')
        self.movieSaveStopAction.setStatusTip('Stop continuous save to disk')
        self.movieSaveStopAction.triggered.connect(self.movieSaveStop)

        self.startWaterfallAction = QtGui.QAction(QtGui.QIcon('View/Icons/Blue-Waterfall-icon.png'),'Start &Waterfall',self)
        self.startWaterfallAction.setShortcut('Ctrl+W')
        self.startWaterfallAction.setStatusTip('Start Waterfall')
        self.startWaterfallAction.triggered.connect(self.startWaterfall)

        self.setROIAction = QtGui.QAction(QtGui.QIcon('View/Icons/Zoom-In-icon.png'),'Set &ROI',self)
        self.setROIAction.setShortcut('Ctrl+T')
        self.setROIAction.setStatusTip('Set ROI')
        self.setROIAction.triggered.connect(self.setROI)

        self.clearROIAction = QtGui.QAction(QtGui.QIcon('View/Icons/Zoom-Out-icon.png'),'Set R&OI',self)
        self.clearROIAction.setShortcut('Ctrl+T')
        self.clearROIAction.setStatusTip('Clear ROI')
        self.clearROIAction.triggered.connect(self.clearROI)

        self.accumulateBufferAction = QtGui.QAction(QtGui.QIcon('View/Icons/disk-save.png'),'Accumulate buffer',self)
        self.accumulateBufferAction.setShortcut('Ctrl+B')
        self.accumulateBufferAction.setStatusTip('Start or stop buffer accumulation')
        self.accumulateBufferAction.triggered.connect(self.bufferStatus)

        self.clearBufferAction = QtGui.QAction('Clear Buffer',self)
        self.clearBufferAction.setShortcut('Ctrl+F')
        self.clearBufferAction.setStatusTip('Clears the buffer')
        self.clearBufferAction.triggered.connect(self.emptyQueue)

        self.viewerAction = QtGui.QAction('Start Viewer',self)
        self.viewerAction.triggered.connect(self.camViewer.show)

        self.configAction = QtGui.QAction('Config Window',self)
        self.configAction.triggered.connect(self.config.show)

    def setupToolbar(self):
        """Setups the toolbar with the desired icons. It's placed into a function
        to make it easier to reuse in other windows.
        """
        self.toolbar = self.addToolBar('Exit')
        self.toolbar.addAction(self.exitAction)
        self.toolbar2 = self.addToolBar('Image')
        self.toolbar2.addAction(self.saveAction)
        self.toolbar2.addAction(self.snapAction)
        self.toolbar3 = self.addToolBar('Movie')
        self.toolbar3.addAction(self.movieAction)
        self.toolbar3.addAction(self.movieSaveStartAction)
        self.toolbar3.addAction(self.movieSaveStopAction)
        self.toolbar4 = self.addToolBar('Waterfall')
        self.toolbar4.addAction(self.startWaterfallAction)
        self.toolbar4.addAction(self.setROIAction)
        self.toolbar4.addAction(self.clearROIAction)

    def bufferStatus(self):
        """Starts or stops the buffer accumulation.
        """
        if self.accumulateBuffer:
            self.accumulateBuffer = False
            self.logMessage.append('<b>Info:</b> Stopped the buffer accumulation')
        else:
            self.accumulateBuffer = True
            self.logMessage.append('<b>Info:</b> Started the buffer accumulation')

    def setupMenubar(self):
        """Setups the menubar.
        """
        menubar = self.menuBar()
        self.fileMenu = menubar.addMenu('&File')
        self.fileMenu.addAction(self.saveAction)
        self.fileMenu.addAction(self.exitAction)
        self.snapMenu = menubar.addMenu('&Snap')
        self.snapMenu.addAction(self.snapAction)
        self.snapMenu.addAction(self.saveAction)
        self.movieMenu = menubar.addMenu('&Movie')
        self.movieMenu.addAction(self.movieAction)
        self.movieMenu.addAction(self.movieSaveStartAction)
        self.movieMenu.addAction(self.movieSaveStopAction)
        self.movieMenu.addAction(self.startWaterfallAction)
        self.configMenu = menubar.addMenu('&Configure')
        self.configMenu.addAction(self.setROIAction)
        self.configMenu.addAction(self.clearROIAction)
        self.configMenu.addAction(self.accumulateBufferAction)
        self.configMenu.addAction(self.clearBufferAction)
        self.configMenu.addAction(self.viewerAction)
        self.configMenu.addAction(self.configAction)
    def getData(self,data,origin):
        """Gets the data that is being gathered by the working thread.
        """
        if origin == 'snap': #Single snap.
            self.acquiring=False
            self.workerThread.origin = None
            self.workerThread.keep_acquiring = False
        self.tempImage = data
        if self.accumulateBuffer:
            self.q.put(data)

        if self.showWaterfall:
            d = np.array([np.sum(data,1)])
            self.watData = np.concatenate((d,self.watData),axis=0)
        self.totalFrames+=1
        new_time = time.time()
        self.bufferTime = new_time - self.lastBuffer
        self.lastBuffer = new_time

    def updateGUI(self):
        """Updates the image displayed to the user.
        """
        if len(self.tempImage) >= 1:
            self.camWidget.img.setImage(self.tempImage)

        if self.showWaterfall:
            self.watData  = self.watData[:self._session.lengthWaterfall,:]
            self.watWidget.img.setImage(np.transpose(self.watData[::-1,:]))

        new_time = time.time()
        self.fps = new_time-self.lastRefresh
        self.lastRefresh = new_time
        if self.q.qsize()/200*100 > 75:
            self.camWidget.memory.setStyleSheet(self.camWidget.RED_STYLE)
        elif self.q.qsize()/200*100 > 50:
            self.camWidget.memory.setStyleSheet(self.camWidget.YELLOW_STYLE)
        else:
            self.camWidget.memory.setStyleSheet(self.camWidget.DEFAULT_STYLE)
        if psutil.cpu_percent() > 75:
            self.camWidget.processor.setStyleSheet(self.camWidget.RED_STYLE)
        else:
            self.camWidget.processor.setStyleSheet(self.camWidget.DEFAULT_STYLE)

        self.camWidget.memory.setValue(self.q.qsize()/200*100)#psutil.virtual_memory().percent*4
        self.camWidget.processor.setValue(psutil.cpu_percent())
        self.camWidget.message.setHtml('<b>Buffer time:</b> %0.2f ms <br /> \
            <b>Refresh time:</b> %0.2f ms <br /> \
            <b>Acquired Frames</b> %i <br /> \
            <b>Frames in buffer</b> %i'%(self.bufferTime*1000,self.fps*1000,self.totalFrames,self.q.qsize()))

        self.logMessage = self.logMessage[-100:]
        logs = ''
        for msg in self.logMessage[::-1]:
            logs+=msg
            logs+='<br />'
        self.camWidget.log.setHtml(logs)

    def updateSession(self,session):
        """Updates the session variables passed by the config window.
        """
        if self.acquiring:
            self.stopMovie()
            self.camera.setExposure(session.exposureTime)
            self.startMovie()
        else:
            self.camera.setExposure(session.exposureTime)
        self.refreshTimer.stop()
        self.refreshTimer.start(session.refreshTime)

        self.camWidget.vline2.setValue(session.ROIl)
        self.camWidget.vline1.setValue(session.ROIr)
        self.camWidget.hline2.setValue(session.ROIu)
        self.camWidget.hline1.setValue(session.ROIb)
        self.logMessage.append('<b>Info:</b> Updated the parameters')
        self._session = session



    def done(self,msg):
        self.saveRunning = False

    def exitSafe(self):
        self.close()

    def closeEvent(self,evnt):
        """Triggered at closing. Checks that the save is complete and closes the dataFile
        """
        if self.acquiring:
            self.stopMovie()
        self.emit(QtCore.SIGNAL('CloseAll'))
        self.camera.stopCamera()
        self.movieSaveStop()
        self.emptyQueue()
        self.close()


class cameraWidget(QtGui.QWidget):
    """Widget for holding the images generated by the camera.
    """
    def __init__(self,parent=None):
        QtGui.QWidget.__init__(self, parent)

        self.layout = QtGui.QVBoxLayout(self)

        self.viewport = GraphicsLayoutWidget()
        self.view = self.viewport.addViewBox(enableMenu=True)
        self.img = pg.ImageItem()
        self.hline1 = pg.InfiniteLine(angle=0, movable=True, hoverPen={'color': "FF0", 'width': 4})
        self.hline2 = pg.InfiniteLine(angle=0, movable=True, hoverPen={'color': "FF0", 'width': 4})
        self.vline1 = pg.InfiniteLine(angle=90, movable=True, hoverPen={'color': "FF0", 'width': 4})
        self.vline2 = pg.InfiniteLine(angle=90, movable=True, hoverPen={'color': "FF0", 'width': 4})
        self.view.addItem(self.img)
        self.view.addItem(self.hline1)
        self.view.addItem(self.hline2)
        self.view.addItem(self.vline1)
        self.view.addItem(self.vline2)


        self.statusBars = QtGui.QHBoxLayout()

        self.memory = QtGui.QProgressBar(self)
        # self.memory.setGeometry(200, 80, 250, 20)

        self.processor = QtGui.QProgressBar(self)
        # self.processor.setGeometry(200,80,250,20)
        self.statusBars.addWidget(self.memory)
        self.statusBars.addWidget(self.processor)

        self.message = QtGui.QTextEdit()
        self.message.setHtml('<b>information</b>')

        self.log = QtGui.QTextEdit()
        self.log.setHtml('<h1>Program Log</h1>')
        # self.message.setGeometry(0, 0, 10, 10)

        self.layout.addWidget(self.viewport)
        self.layout.addLayout(self.statusBars)
        self.layout.addWidget(self.message)
        self.layout.addWidget(self.log)
        self.setLayout(self.layout)

        self.RED_STYLE = """
        QProgressBar{
            border: 2px solid grey;
            border-radius: 5px;
            text-align: center
        }

        QProgressBar::chunk {
            background-color: red;
            width: 10px;
            margin: 1px;
        }
        """
        self.DEFAULT_STYLE = """
        QProgressBar{
            border: 2px solid grey;
            border-radius: 5px;
            text-align: center
        }

        QProgressBar::chunk {
            background-color: green;
            width: 10px;
            margin: 1px;
        }
        """

        self.YELLOW_STYLE = """
        QProgressBar{
            border: 2px solid grey;
            border-radius: 5px;
            text-align: center
        }

        QProgressBar::chunk {
            background-color: yellow;
            width: 10px;
            margin: 1px;
        }
        """






if __name__ == '__main__':
    app = QApplication(sys.argv)
    cam = cameraMain()
    cam.show()
    sys.exit(app.exec_())
