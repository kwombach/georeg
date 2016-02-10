# coding=utf-8
import cv2
import numpy as np
import tempfile
import difflib
import re
import os
import csv
import sys
import subprocess
import ConfigParser
import itertools
from operator import itemgetter, attrgetter
from sklearn.cluster import KMeans

import georeg

_datadir = os.path.join(georeg.__path__[0], "data")

class CityDetector:
    """loads a file of cities for comparison against strings"""
    def __init__(self):
        self.city_list = []
        
    def load_cities(self, file_name):
        self.city_list = [] # clear old values

        with open(file_name) as file:
            for line in file:
                line = line.strip()
                self.city_list.append(line)

    def match_to_cities(self, line, cutoff = 0.6):
        line = line.lower().strip()

        # '—' not easily expressed in ascii
        em_dash = '\xe2\x80\x94'

        # if the end of the string matches "—continued" then remove it
        if len(difflib.get_close_matches(line[-12:], [em_dash + "continued"], cutoff=cutoff)) > 0:
            line = line[:-12]

        match_list = difflib.get_close_matches(line, self.city_list, cutoff = cutoff)
        return match_list

class Business:

    def __init__(self):
        self.name = ""
        self.city = ""
        self.zip = ""
        self.address = ""
        self.category = [] # business category or sic code depending on year
        self.emp = "" # employment
        self.sales = ""
        self.cat_desc = []
        self.bracket = ""

        # coordinates
        self.lat = ""
        self.long = ""
        self.confidence_score = 0.0

class Contour:
    def __init__(self, contour = None):
        self.data = contour
        
        if contour is not None:
            [self.x,self.y,self.w,self.h] = cv2.boundingRect(contour)
            self.x_mid = self.x + self.w / 2
            self.y_mid = self.y + self.h / 2
        else:
            self.x = 0
            self.y = 0
            self.w = 0
            self.h = 0
    
            self.x_mid = 0
            self.y_mid = 0

# a special exception class for exceptions generated by registry processor and child classes
class RegistryProcessorException(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return self.value

class RegistryProcessor(object):
    def __init__(self, state, year):
        self._image = None
        self._image_height = lambda: self._image.shape[0]
        self._image_width = lambda: self._image.shape[1]
        self._thresh = None

        # image processing parameters (these are example values)
        self.kernel_shape = (10,3)
        self.thresh_value = 60 # higher = more exposure (max = 255) 
        self.iterations = 8
        self.match_rate = 0.7 # lower = more lenient
        self.indent_width = 0.025 # indent width as % of contour width

        # percent of image width and height to add to bounding box width and height of contours (improves ocr accuracy)
        # higher = bigger bounding box
        self.bb_expansion_percent = 0.012

        self._expand_bb = lambda x,y,w,h: (x - int(round(self._image_width() * self.bb_expansion_percent / 2.0)), \
                                           y - int(round(self._image_height() * self.bb_expansion_percent / 2.0)), \
                                           w + int(round(self._image_width() * self.bb_expansion_percent)), \
                                           h + int(round(self._image_height() * self.bb_expansion_percent)))

        self.columns_per_page = 2
        self.pages_per_image = 1

        self.seed = 0 # seed value for k-means clustering
        self.std_thresh = 1 # number of standard deviations beyond which contour is no longer considered part of column

        self.draw_debug_images = False # turning this on can help with debugging
        self.assume_pre_processed = False # assume images are preprocessed so to not waste extra computational power
        self.line_color = (130,130,130) # NOTE: line color for debug images, must be visible in grayscale
        self.debugdir = "" # dir to which to write debug images

        self.businesses = []
        self.__tmp_path = tempfile.mktemp(suffix=".tiff")

        # city lookup
        self.state = state
        self._city_lists_path = os.path.join(_datadir, "%s-cities.txt" % (self.state,))
        self._city_detector = CityDetector()
        self._city_detector.load_cities(self._city_lists_path)

        # load config file
        basepath = georeg.__path__[0]
        filename = str(year) + '.cfg'
        path = os.path.abspath(os.path.join(basepath, "configs", state, filename))
        if os.path.exists(path):
            self.load_settings_from_cfg(path)
        else:
            print >>sys.stderr, "configuration file not found"
     

    def __del__(self):
        # clean up our temp files
        if os.path.isfile(self.__tmp_path):
            os.remove(self.__tmp_path)
        if os.path.isfile(self.__tmp_path + ".txt"):
            os.remove(self.__tmp_path + ".txt")

    def process_image(self, path):
        """this is a wrapper for _process_image() which catches exceptions and reports them"""
        try:
            self._process_image(path)
        except RegistryProcessorException as e:
            print >>sys.stderr, "error: %s, skipping" % e

    def _process_image(self, path):
        """process a registry image and store results in the businesses member,
        don't call this directly call process_image() instead"""

        self.businesses = [] # reset businesses list

        self._image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)

        _,contours,_ = self._get_contours(self.kernel_shape, self.iterations, True)
        contours = [Contour(c) for c in contours]

        # remove noise from edge of image
        if not self.assume_pre_processed:
            contours = self._remove_edge_contours(contours)

        if self.draw_debug_images:
            canvas = np.zeros(self._image.shape,self._image.dtype)
            cv2.drawContours(canvas,[c.data for c in
                contours],-1,self.line_color,-1)
            cv2.imwrite(os.path.join(self.debugdir, "closed.tiff"),canvas)

        clustering = self._find_column_locations(contours)
        columns, _ = self._assemble_contour_columns(contours, clustering)
        contours = list(itertools.chain.from_iterable(columns))

        contoured = None

        if self.draw_debug_images:
            contoured = self._image.copy()

        for contour in contours:
            x,y,w,h = self._expand_bb(contour.x,contour.y,contour.w,contour.h)

            if self.draw_debug_images:
                # draw bounding box on original image
                cv2.rectangle(contoured,(x,y),(x+w,y+h),self.line_color,5)

            cropped = self._thresh[y:y+h, x:x+w]
            contour_txt = self._ocr_image(cropped)

            self._process_contour(contour_txt)

        if self.draw_debug_images:
            # write original image with added contours to disk
            cv2.imwrite(os.path.join(self.debugdir, "contoured.tiff"), contoured)

    def _process_contour(self, contour_txt):
        """perform pre-processing and ocr on contour"""

        raise NotImplementedError

    def load_from_tsv(self, path):
        """load self.businesses from a tsv file where they were previously saved"""

        self.businesses = [] # reset businesses list

        # mini function for loading an individual business file
        def load_businesses(path):
            with open(path, "r") as file:
                file_reader = csv.reader(file, delimiter="\t")
                for row in file_reader:
                    business = Business()

                    [business.category, business.name, business.city,
                     business.address, business.zip, business.emp,
                     business.lat, business.long, business.confidence_score] = row

                    # cast to float
                    business.confidence_score = float(business.confidence_score)

                    self.businesses.append(business)

        # load normal businesses
        load_businesses(path, False)

    def record_to_tsv(self, path, mode = 'w'):
        """record business registries to tsv, opened with file access mode: mode"""

        with open(path, mode) as file:
            file_writer = csv.writer(file, delimiter ="\t")

            for business in self.businesses:
                entry = [business.category, business.name, business.address, business.city, business.zip, business.emp, business.sales, business.cat_desc, business.bracket, business.lat, business.long, business.confidence_score]

                file_writer.writerow(entry)

    def load_settings_from_cfg(self, path):
        # Set default values.
        cp = ConfigParser.ConfigParser({
                'kernel_shape_x': str(self.kernel_shape),
                'thresh_value': str(self.thresh_value),
                'iterations': str(self.iterations),
                'match_rate': str(self.match_rate),
                'columns_per_page': str(self.columns_per_page),
                'pages_per_image': str(self.pages_per_image),
                'bb_expansion_percent': str(self.bb_expansion_percent), 
                'indent_width': str(self.indent_width),
                'std_thresh': str(self.std_thresh),
            })
        cp.read(path)

        # Get values from config file.
        self.kernel_shape = (int(cp.get('RegistryProcessor','kernel_shape_x')),int(cp.get('RegistryProcessor','kernel_shape_y')))
        self.thresh_value = cp.getint('RegistryProcessor','thresh_value')
        self.iterations = cp.getint('RegistryProcessor','iterations')
        self.match_rate = cp.getfloat('RegistryProcessor','match_rate')
        self.columns_per_page = cp.getint('RegistryProcessor','columns_per_page')
        self.pages_per_image = cp.getint('RegistryProcessor','pages_per_image')
        self.bb_expansion_percent = cp.getfloat('RegistryProcessor','bb_expansion_percent')
        self.indent_width = cp.getfloat('RegistryProcessor','indent_width')
        self.std_thresh = cp.getfloat('RegistryProcessor','std_thresh')

    def save_settings_to_cfg(self, path):
        cp = ConfigParser.SafeConfigParser()

        cp.add_section('RegistryProcessor')
        cp.set('RegistryProcessor','kernel_shape_x',str(self.kernel_shape[0]))
        cp.set('RegistryProcessor','kernel_shape_y',str(self.kernel_shape[1]))
        cp.set('RegistryProcessor','thresh_value',str(self.thresh_value))
        cp.set('RegistryProcessor','iterations',str(self.iterations))
        cp.set('RegistryProcessor','match_rate',str(self.match_rate))
        cp.set('RegistryProcessor','columns_per_page',str(self.columns_per_page))
        cp.set('RegistryProcessor','pages_per_image',str(self.pages_per_image))
        cp.set('RegistryProcessor','bb_expansion_percent',str(self.bb_expansion_percent))
        cp.set('RegistryProcessor','indent_width',str(self.indent_width))
        cp.set('RegistryProcessor','std_thresh',str(self.std_thresh))

        with open(path,'w') as cfg_file:
            cp.write(cfg_file)

    def _remove_edge_contours(self, contours):
        """remove contours that touch the edge of image
        and crops self._image and self._thresh to an
        appropriate size"""

        filtered_contours = []

        for contour in contours:
            if (contour.x == 1 or contour.x + contour.w == self._image_width() - 1) or \
            (contour.y == 1 or contour.y + contour.h == self._image_height() - 1):
                continue

            filtered_contours.append(contour)

        if len(filtered_contours) == 0:
            raise RegistryProcessorException("No non-background contours found, check debug images")

        # create cropped version of
        super_contour = np.concatenate([c.data for c in filtered_contours])
        [x,y,w,h] = cv2.boundingRect(super_contour)

        # make bounding box bigger
        x,y,w,h = self._expand_bb(x,y,w,h)

        self._image = self._image[y:y+h,x:x+w]
        self._thresh = self._thresh[y:y+h,x:x+w]

        # apply cropping offset to contours
        for c in contours:
            c.x -= x
            c.x_mid -= x
            c.y -= y
            c.y_mid -= y
            # apply cropping offset to each point in contours
            for p in c.data:
                p[0][0] -= x
                p[0][1] -= y

        return filtered_contours

    def _get_contours(self, kernel_shape, iter, make_new_thresh = True):
        """performs a close operation on self._image then extracts the contours"""

        if make_new_thresh: # if thresh_value is provided then we make a new thresh image
            if not self.assume_pre_processed:
                _,self._thresh = cv2.threshold(self._image,self.thresh_value,255,cv2.THRESH_BINARY_INV) # threshold
            else:
                _,self._thresh = cv2.threshold(self._image,0,255,cv2.THRESH_BINARY_INV) # threshold with 0 threshold value

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT,kernel_shape)
        closed = cv2.morphologyEx(self._thresh,cv2.MORPH_CLOSE,kernel,iterations = iter) # close

        # perform a small open operation to remove noise
        closed = cv2.morphologyEx(closed,cv2.MORPH_OPEN,kernel,iterations = iter / 3)

        return cv2.findContours(closed,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE) # get contours

    def _ocr_image(self, image):
        """use tesseract to ocr a black and white image and return the text"""
        # write image to file
        cv2.imwrite(self.__tmp_path, image)

        # call tesseract on image
        # (Popen with piped streams hides tesseract output)
        p = subprocess.Popen(["tesseract", self.__tmp_path, self.__tmp_path, "-psm", "6"], stdout = subprocess.PIPE, stderr = subprocess.PIPE)
        p.wait()

        contour_txt = ""

        for line in open(self.__tmp_path + ".txt"):
            if not re.match(r'^\s*$', line):
                contour_txt = contour_txt + line

        return contour_txt.strip()

    def _find_column_locations(self, contours):
        """find column column locations, and page boundary if two pages
        (returns column locations)"""

        # create array of coords for left and right edges of contours
        coords = [[contour.x, contour.x + contour.w] for contour in contours]
        coords_arr = np.array(coords)

        # use k-means clustering to get column boundaries for expected # of cols
        num_cols = self.columns_per_page * self.pages_per_image
        k_means = KMeans(n_clusters=num_cols, random_state=self.seed)
        clustering = k_means.fit(coords_arr)

        # draw columns lines
        if self.draw_debug_images:
            canvas = self._thresh.copy()

            # use left and right coords of clusters to draw columns
            for column_l in clustering.cluster_centers_:
                left, right = int(column_l[0]), int(column_l[1])
                cv2.line(canvas,(left, 0),(left,
                    self._image_height()),self.line_color,20)
                cv2.line(canvas,(right, 0),(right,
                    self._image_height()),self.line_color,20)

            # draw column lines to file
            cv2.imwrite(os.path.join(self.debugdir, "column_lines.tiff"), canvas)

        return clustering

    def _assemble_contour_columns(self, contours, clustering):
        """assemble contours into columns and seperate those that dont belong
        to a column (returns contours sorted by column and position)"""

        column_contours = []
        non_column_contours = []

        # sort contours by clusters
        cluster_groups = sorted(zip(clustering.labels_, contours))

        # iterate through column groups, deciding which contours are valid
        for col_ix, cluster_group in itertools.groupby(cluster_groups, lambda x: x[0]):

            # coords of column
            col_loc = clustering.cluster_centers_[col_ix]

            cluster_contours = [c for _, c in cluster_group]

            # x-coords of contours
            contour_locs = [[c.x, c.x + c.w] for c in cluster_contours]
            # calculate standard deviation of contour x-coords
            col_std = np.std(contour_locs)

            # only keep contours if less than threshold std devs from column
            keep_contours = []
            for contour_ix, contour in enumerate(cluster_contours):
                dist = abs(np.linalg.norm(contour_locs[contour_ix] - col_loc))
                if dist < self.std_thresh * col_std:
                    keep_contours.append(contour)
                else:
                    non_column_contours.append(contour)

            column_contours.append(keep_contours)

        # sort column and contour by position
        sorted_contours = sorted(enumerate(column_contours),key=lambda x: 
                                 clustering.cluster_centers_[x[0]][0])
        for i, column in sorted_contours:
            column_contours[i] = sorted(column,key=attrgetter('y'))

        return column_contours, non_column_contours
